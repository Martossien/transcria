"""Diarisation Nemotron 3 (audio.cpp, sous-process) — post-traitement pur + backend à runner injecté.

Aucun binaire ni GPU : le format des tours est celui relevé sur le binaire réel
(``audiocpp_cli --turns-out``, 2026-09-30) — échantillons 16 kHz, ``speaker_N``, confidence.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from transcria.stt.diarizer_factory import (
    BACKEND_MAX_SPEAKERS,
    apply_speaker_hint,
    create_diarizer,
    get_diarizer_vram_mb,
    list_available_backends,
)
from transcria.stt.nemotron_diarizer import (
    NEMOTRON_MAX_SPEAKERS,
    NemotronDiarizer,
    build_result,
    exclusive_turns,
    fold_minor_speakers,
    parse_turns,
    renumber_by_arrival,
)


def _raw(start_s: float, end_s: float, speaker: str, confidence: float = 0.9) -> dict:
    return {"start_sample": int(start_s * 16000), "end_sample": int(end_s * 16000),
            "speaker_id": speaker, "confidence": confidence}


def _turn(start: float, end: float, speaker: str, confidence: float = 0.9) -> dict:
    return {"start": start, "end": end, "speaker": speaker, "confidence": confidence}


class TestParseTurns:
    def test_samples_become_seconds(self):
        turns = parse_turns([_raw(2.51, 4.13, "speaker_0", 0.618)])
        assert turns == [{"start": 2.51, "end": 4.13, "speaker": "speaker_0", "confidence": 0.618}]

    def test_sorted_by_start_whatever_the_file_order(self):
        turns = parse_turns([_raw(5, 6, "speaker_1"), _raw(1, 2, "speaker_0")])
        assert [t["speaker"] for t in turns] == ["speaker_0", "speaker_1"]

    @pytest.mark.parametrize("payload", [None, {}, "texte", 42, [None, "x", 3]])
    def test_foreign_payloads_give_no_turn(self, payload):
        assert parse_turns(payload) == []

    def test_malformed_and_empty_entries_are_skipped(self):
        payload = [
            {"start_sample": 0, "end_sample": 16000},                                   # sans locuteur
            {"start_sample": "x", "end_sample": 16000, "speaker_id": "speaker_0"},      # début illisible
            _raw(3, 3, "speaker_0"),                                                    # durée nulle
            _raw(4, 2, "speaker_0"),                                                    # fin avant début
            {"start_sample": 0, "end_sample": 8000, "speaker_id": "speaker_1", "confidence": "?"},
        ]
        assert parse_turns(payload) == [{"start": 0.0, "end": 0.5, "speaker": "speaker_1", "confidence": 0.0}]


class TestFoldMinorSpeakers:
    def test_a_phantom_speaker_joins_its_nearest_neighbour(self):
        turns = [_turn(0, 10, "speaker_0"), _turn(10.2, 10.9, "speaker_2"), _turn(30, 40, "speaker_1")]
        folded = fold_minor_speakers(turns, min_total_s=2.0)
        assert [t["speaker"] for t in folded] == ["speaker_0", "speaker_0", "speaker_1"]
        assert folded[1]["start"] == 10.2 and folded[1]["end"] == 10.9      # l'audio reste couvert

    def test_nothing_is_dropped(self):
        turns = [_turn(0, 10, "speaker_0"), _turn(11, 11.5, "speaker_3"), _turn(12, 12.4, "speaker_3")]
        assert len(fold_minor_speakers(turns, min_total_s=2.0)) == 3

    def test_several_short_turns_that_add_up_make_a_real_speaker(self):
        turns = [_turn(0, 10, "speaker_0")] + [_turn(20 + i, 20.6 + i, "speaker_1") for i in range(4)]
        folded = fold_minor_speakers(turns, min_total_s=2.0)                # 4 × 0,6 s = 2,4 s
        assert {t["speaker"] for t in folded} == {"speaker_0", "speaker_1"}

    def test_without_any_major_speaker_nothing_changes(self):
        turns = [_turn(0, 0.5, "speaker_0"), _turn(1, 1.4, "speaker_1")]
        assert fold_minor_speakers(turns, min_total_s=2.0) == turns

    def test_returns_copies(self):
        turns = [_turn(0, 10, "speaker_0")]
        assert fold_minor_speakers(turns, min_total_s=2.0)[0] is not turns[0]


class TestExclusiveTurns:
    def test_no_overlap_left(self):
        turns = [_turn(0, 10, "speaker_0", 0.9), _turn(8, 14, "speaker_1", 0.95), _turn(13, 20, "speaker_0", 0.9)]
        out = exclusive_turns(turns)
        assert all(a["end"] <= b["start"] for a, b in zip(out, out[1:], strict=False))
        assert out[0]["start"] == 0 and out[-1]["end"] == 20                # toute la parole reste couverte

    def test_the_more_confident_voice_wins_the_overlap(self):
        out = exclusive_turns([_turn(0, 10, "speaker_0", 0.6), _turn(8, 12, "speaker_1", 0.9)])
        assert out == [{"start": 0, "end": 8, "speaker": "speaker_0"}, {"start": 8, "end": 12, "speaker": "speaker_1"}]

    def test_an_uncertain_voice_does_not_cut_the_ongoing_speaker(self):
        out = exclusive_turns([_turn(0, 10, "speaker_0", 0.9), _turn(4, 6, "speaker_1", 0.55)])
        assert out == [{"start": 0, "end": 10, "speaker": "speaker_0"}]

    def test_equal_confidence_keeps_whoever_was_speaking(self):
        out = exclusive_turns([_turn(0, 10, "speaker_0", 0.8), _turn(6, 12, "speaker_1", 0.8)])
        assert out == [{"start": 0, "end": 10, "speaker": "speaker_0"}, {"start": 10, "end": 12, "speaker": "speaker_1"}]

    def test_same_speaker_pieces_merge_across_a_short_gap_only(self):
        merged = exclusive_turns([_turn(0, 2, "speaker_0"), _turn(2.2, 4, "speaker_0")], merge_gap_s=0.35)
        assert merged == [{"start": 0, "end": 4, "speaker": "speaker_0"}]
        split = exclusive_turns([_turn(0, 2, "speaker_0"), _turn(3, 4, "speaker_0")], merge_gap_s=0.35)
        assert len(split) == 2

    def test_a_gap_is_not_bridged_over_another_speaker(self):
        out = exclusive_turns([_turn(0, 2, "speaker_0"), _turn(2.05, 2.2, "speaker_1"), _turn(2.25, 4, "speaker_0")])
        assert [t["speaker"] for t in out] == ["speaker_0", "speaker_1", "speaker_0"]

    def test_empty(self):
        assert exclusive_turns([]) == []


class TestRenumbering:
    def test_continuous_ids_in_order_of_first_appearance(self):
        mapping = renumber_by_arrival([_turn(5, 6, "speaker_3"), _turn(0, 1, "speaker_1"), _turn(9, 10, "speaker_0")])
        assert mapping == {"speaker_1": "SPEAKER_00", "speaker_3": "SPEAKER_01", "speaker_0": "SPEAKER_02"}


class TestBuildResult:
    def test_canonical_contract(self):
        raw = [_turn(0, 10, "speaker_0", 0.9), _turn(8, 14, "speaker_2", 0.95), _turn(20, 20.5, "speaker_5", 0.6)]
        result = build_result(raw, min_speaker_total_s=2.0, merge_gap_s=0.35)
        assert result["available"] is True
        assert result["speakers"] == ["SPEAKER_00", "SPEAKER_01"]           # le fantôme est rattaché
        assert set(result["turns"][0]) == {"start", "end", "speaker", "duration"}
        excl = result["exclusive_turns"]
        assert all(a["end"] <= b["start"] for a, b in zip(excl, excl[1:], strict=False))
        assert result["stats"]["SPEAKER_00"] == {"speaking_time_seconds": 8.0, "turn_count": 1}
        assert sum(s["turn_count"] for s in result["stats"].values()) == len(excl)


# ── Backend (runner injecté) ────────────────────────────────────────────────────


def _install(tmp_path: Path) -> dict:
    cli = tmp_path / "audiocpp_cli"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(0o755)
    model = tmp_path / "nemotron-3-diarization-bf16.gguf"
    model.write_bytes(b"gguf")
    return {"nemotron_diar": {"cli_path": str(cli), "model_path": str(model)}}


def _runner(turns, *, returncode=0, stdout="", calls=None):
    def run(cmd, **kwargs):
        if calls is not None:
            calls.append((cmd, kwargs))
        if turns is not None:
            Path(cmd[cmd.index("--turns-out") + 1]).write_text(json.dumps(turns), encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")
    return run


class TestBackend:
    def test_unavailable_without_the_binary_or_the_model(self, tmp_path):
        diarizer = NemotronDiarizer({"nemotron_diar": {"cli_path": str(tmp_path / "absent"),
                                                       "model_path": str(tmp_path / "absent.gguf")}})
        assert diarizer.available is False
        result = diarizer.diarize_audio(tmp_path / "a.wav")
        assert result["available"] is False and "introuvable" in result["message"]

    def test_gpu_reserved_by_the_phase_is_passed_as_device(self, tmp_path):
        calls: list = []
        diarizer = NemotronDiarizer(_install(tmp_path), device="cuda:3", runner=_runner([_raw(0, 5, "speaker_0")], calls=calls))
        result = diarizer.diarize_audio(tmp_path / "a.wav")
        cmd = calls[0][0]
        assert cmd[cmd.index("--backend") + 1] == "cuda" and cmd[cmd.index("--device") + 1] == "3"
        assert cmd[cmd.index("--task") + 1] == "diar" and cmd[cmd.index("--family") + 1] == "nemotron_3_diar"
        assert result["speakers"] == ["SPEAKER_00"]

    def test_cpu_when_no_gpu_was_reserved(self, tmp_path):
        calls: list = []
        NemotronDiarizer(_install(tmp_path), device="cpu", runner=_runner([], calls=calls)).diarize_audio(tmp_path / "a.wav")
        cmd = calls[0][0]
        assert cmd[cmd.index("--backend") + 1] == "cpu" and "--device" not in cmd

    def test_forced_cpu_ignores_the_gpu(self, tmp_path):
        cfg = _install(tmp_path)
        cfg["nemotron_diar"]["backend"] = "cpu"
        calls: list = []
        NemotronDiarizer(cfg, device="cuda:0", runner=_runner([], calls=calls)).diarize_audio(tmp_path / "a.wav")
        assert calls[0][0][calls[0][0].index("--backend") + 1] == "cpu"
        assert get_diarizer_vram_mb("nemotron_diar", cfg) == 0            # donc aucune VRAM réservée

    def test_no_turn_is_an_empty_but_available_result(self, tmp_path):
        result = NemotronDiarizer(_install(tmp_path), runner=_runner([])).diarize_audio(tmp_path / "a.wav")
        assert result == {"available": True, "turns": [], "exclusive_turns": [], "speakers": [], "stats": {}}

    def test_cli_failure_is_reported_never_raised(self, tmp_path):
        run = _runner(None, returncode=1, stdout="audiocpp_cli failed: input is MP3, not WAV")
        result = NemotronDiarizer(_install(tmp_path), runner=run).diarize_audio(tmp_path / "a.mp3")
        assert result["available"] is False and "not WAV" in result["error"]

    def test_timeout_is_reported_never_raised(self, tmp_path):
        def run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])
        result = NemotronDiarizer(_install(tmp_path), runner=run).diarize_audio(tmp_path / "a.wav")
        assert result["available"] is False and "délai" in result["error"]

    def test_unreadable_turn_file_is_reported(self, tmp_path):
        def run(cmd, **kwargs):
            Path(cmd[cmd.index("--turns-out") + 1]).write_text("{ cassé", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        result = NemotronDiarizer(_install(tmp_path), runner=run).diarize_audio(tmp_path / "a.wav")
        assert result["available"] is False and "illisibles" in result["error"]

    def test_model_name_identifies_the_weights_for_the_checkpoint(self, tmp_path):
        assert NemotronDiarizer(_install(tmp_path)).model_name == "nemotron-3-diarization:nemotron-3-diarization-bf16.gguf"


class TestFactory:
    def test_backend_is_registered(self, tmp_path):
        assert "nemotron_diar" in list_available_backends()
        cfg = {**_install(tmp_path), "models": {"diarization_backend": "nemotron_diar"}}
        assert isinstance(create_diarizer(cfg, device="cuda:1"), NemotronDiarizer)

    def test_default_vram_comes_from_the_loader(self):
        assert get_diarizer_vram_mb("nemotron_diar", {}) == 2000

    def test_capacity_is_eight_speakers(self):
        assert NEMOTRON_MAX_SPEAKERS == 8 and BACKEND_MAX_SPEAKERS["nemotron_diar"] == 8

    @pytest.mark.parametrize("upper,expected", [(8, "nemotron_diar"), (9, "pyannote")])
    def test_speaker_hint_falls_back_to_pyannote_only_beyond_eight(self, upper, expected):
        cfg = apply_speaker_hint({"models": {"diarization_backend": "nemotron_diar"}}, {"min": 2, "max": upper})
        assert cfg["models"]["diarization_backend"] == expected

    def test_sortformer_still_falls_back_beyond_four(self):
        cfg = apply_speaker_hint({"models": {"diarization_backend": "sortformer"}}, {"max": 5})
        assert cfg["models"]["diarization_backend"] == "pyannote"


# ── Localisation du binaire : build CUDA (bin/) avant précompilé (prebuilt/) ──

from transcria.stt import nemotron_diarizer as nd  # noqa: E402 — bloc 0.4.7


def _exe(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class TestFindAudiocppCli:
    def test_candidats_dans_l_ordre(self, tmp_path):
        first, second = nd.audiocpp_cli_candidates(tmp_path)
        assert first == tmp_path / "audiocpp" / "bin" / "audiocpp_cli"
        assert second == tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli"

    def test_aucun_binaire(self, tmp_path):
        assert nd.find_audiocpp_cli(tmp_path) is None

    def test_precompile_seul(self, tmp_path):
        cli = _exe(tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli")
        assert nd.find_audiocpp_cli(tmp_path) == cli

    def test_build_cuda_prefere(self, tmp_path):
        _exe(tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli")
        cuda = _exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")
        assert nd.find_audiocpp_cli(tmp_path) == cuda

    def test_non_executable_ignore(self, tmp_path):
        cli = tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli"
        cli.parent.mkdir(parents=True)
        cli.write_text("x")
        cli.chmod(0o644)
        assert nd.find_audiocpp_cli(tmp_path) is None

    def test_le_diariseur_suit_le_localisateur(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
        cli = _exe(tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli")
        d = nd.NemotronDiarizer({"nemotron_diar": {"model_path": str(tmp_path / "m.gguf")}})
        assert d._cli == cli
        assert d.available is False                          # GGUF absent
        (tmp_path / "m.gguf").write_bytes(b"x")
        assert nd.nemotron_diar_available({"nemotron_diar": {"model_path": str(tmp_path / "m.gguf")}}) is True

    def test_sans_binaire_le_message_nomme_la_page_modeles(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
        d = nd.NemotronDiarizer({"nemotron_diar": {"model_path": str(tmp_path / "m.gguf")}})
        assert d._cli == tmp_path / "audiocpp" / "bin" / "audiocpp_cli"
        assert "Modèles" in d._unavailable()["message"] and "nemotron-diar" in d._unavailable()["message"]


class TestPrebuiltIsCpuOnly:
    """Gate d'installation 2026-10-10 : le précompilé refuse `--backend cuda`."""

    def _prebuilt(self, tmp_path):
        cli = _exe(tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli")
        (cli.parent / nd.PREBUILT_MARKER).write_text("v0.9.0\n")
        return cli

    def test_detection_par_marqueur(self, tmp_path):
        cli = self._prebuilt(tmp_path)
        assert nd.cli_is_cpu_only(cli) is True
        assert nd.cli_is_cpu_only(_exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")) is False

    def test_reste_sur_cpu_meme_avec_un_gpu_reserve(self, tmp_path):
        cli = self._prebuilt(tmp_path)
        d = nd.NemotronDiarizer({"nemotron_diar": {"cli_path": str(cli), "backend": "auto"}}, device="cuda:3")
        assert d._backend_args() == ["--backend", "cpu"]
        d = nd.NemotronDiarizer({"nemotron_diar": {"cli_path": str(cli), "backend": "cuda"}}, device="cuda:3")
        assert d._backend_args() == ["--backend", "cpu"]

    def test_build_cuda_garde_le_gpu(self, tmp_path):
        cli = _exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")
        d = nd.NemotronDiarizer({"nemotron_diar": {"cli_path": str(cli), "backend": "auto"}}, device="cuda:3")
        assert d._backend_args() == ["--backend", "cuda", "--device", "3"]

    def test_aucune_vram_reservee_pour_le_precompile(self, tmp_path):
        from transcria.stt.diarizer_factory import get_diarizer_vram_mb

        cli = self._prebuilt(tmp_path)
        cfg = {"nemotron_diar": {"cli_path": str(cli)}, "gpu": {"nemotron_diar_vram_mb": 2000}}
        assert get_diarizer_vram_mb("nemotron_diar", cfg) == 0
        cfg["nemotron_diar"]["cli_path"] = str(_exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli"))
        assert get_diarizer_vram_mb("nemotron_diar", cfg) == 2000


class TestLoaderFailureFallsBackToPrebuilt:
    """Gate 2026-10-10 : build CUDA présent mais « error while loading shared libraries »."""

    def _layout(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
        cuda = _exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")
        pre = _exe(tmp_path / "audiocpp" / "prebuilt" / "audiocpp_cli")
        (pre.parent / nd.PREBUILT_MARKER).write_text("v0.9.0\n")
        (tmp_path / "m.gguf").write_bytes(b"x")
        return cuda, pre

    def test_code_127_puis_precompile_cpu(self, tmp_path, monkeypatch):
        import json
        from types import SimpleNamespace

        cuda, pre = self._layout(tmp_path, monkeypatch)
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            if cmd[0] == str(cuda):
                return SimpleNamespace(returncode=127, stderr="error while loading shared libraries: libcublas.so.13", stdout="")
            out = Path(cmd[cmd.index("--turns-out") + 1])
            out.write_text(json.dumps([{"start_sample": 0, "end_sample": 16000, "speaker_id": "speaker_0", "confidence": 0.9}]))
            return SimpleNamespace(returncode=0, stderr="", stdout="")

        d = nd.NemotronDiarizer({"nemotron_diar": {"model_path": str(tmp_path / "m.gguf")}}, device="cuda:0", runner=run)
        res = d.diarize_audio(tmp_path / "a.wav")
        assert res["available"] is True and res["speakers"] == ["SPEAKER_00"]
        assert [c[0] for c in calls] == [str(cuda), str(pre)]
        assert calls[1][calls[1].index("--backend") + 1] == "cpu"
        assert d._cli == pre                                  # les appels suivants vont droit au précompilé

    def test_sans_precompile_l_echec_est_remonte(self, tmp_path, monkeypatch):
        from types import SimpleNamespace

        monkeypatch.setenv("TRANSCRIA_RUNTIMES_DIR", str(tmp_path))
        cuda = _exe(tmp_path / "audiocpp" / "bin" / "audiocpp_cli")
        (tmp_path / "m.gguf").write_bytes(b"x")
        run = lambda cmd, **kw: SimpleNamespace(returncode=127, stderr="loading shared libraries", stdout="")  # noqa: E731
        d = nd.NemotronDiarizer({"nemotron_diar": {"model_path": str(tmp_path / "m.gguf")}}, runner=run)
        res = d.diarize_audio(tmp_path / "a.wav")
        assert res["available"] is False and "code 127" in res["error"] and d._cli == cuda
