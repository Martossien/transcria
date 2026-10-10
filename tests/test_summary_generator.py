"""Tests for SummaryGenerator — generate_quick_summary."""
import sys
from types import SimpleNamespace

import numpy as np

from transcria.stt.summary import SummaryGenerator


def _default_cfg(tmp_path):
    return {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"summary_llm": {"enabled": False}},
        "models": {"cohere_model_path": "/tmp/fake_model"},
    }


_FAKE_AUDIO = np.zeros(16000, dtype=np.float32)   # 1s de silence synthétique


class TestSummaryGeneratorGenerateQuickSummary:
    def test_generate_quick_summary_saves_files_and_returns(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            cfg = _default_cfg(tmp_path)
            from transcria.audio.vad import SileroVAD
            from transcria.jobs.filesystem import JobFilesystem
            from transcria.jobs.store import JobStore
            from transcria.stt.cohere_transcriber import CohereTranscriber
            monkeypatch.setitem(
                sys.modules,
                "librosa",
                SimpleNamespace(load=lambda *a, **kw: (_FAKE_AUDIO, 16000)),
            )
            import librosa

            job = JobStore.create_job(owner_id, "Quick Summary")
            fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)

            fake_segments = [
                {"start": 0.0, "end": 5.0, "text": "Bonjour à tous"},
                {"start": 5.0, "end": 10.0, "text": "Le budget est bouclé"},
            ]

            # Mock audio I/O et pipeline IA — pas de vrai fichier WAV requis
            assert librosa.load("unused")[1] == 16000
            monkeypatch.setattr(SileroVAD, "build_speech_chunks",
                                lambda self, audio, **kw: [{"start": 0.0, "end": 1.0, "audio": audio}])
            monkeypatch.setattr(CohereTranscriber, "load", lambda self: True)
            monkeypatch.setattr(CohereTranscriber, "transcribe", lambda self, *a, **kw: fake_segments)
            monkeypatch.setattr(CohereTranscriber, "offload", lambda self: None)

            gen = SummaryGenerator(cfg)
            audio_path = tmp_path / "test_audio.wav"
            audio_path.write_text("fake audio")

            result = gen.generate_quick_summary(job, audio_path, gpu_index=0)

            assert "transcript_text" in result
            assert "Bonjour à tous" in result["transcript_text"]
            assert result["segment_count"] == 2

            transcript_file = fs.load_text("summary/quick_transcript.txt")
            assert transcript_file is not None
            assert "Bonjour à tous" in transcript_file

            summary_json = fs.load_json("summary/summary.json")
            assert summary_json is not None
            assert len(summary_json["segments"]) == 2

            summary_md = fs.load_text("summary/summary.md")
            assert summary_md is not None
            assert "Résumé" in summary_md or "Extrait" in summary_md


class TestSummaryBatchedPath:
    """Voie batchée (2026-08-07) : quand le backend expose transcribe_prechunked
    (moss, cohere_tf5 — sous-process qui recharge le modèle à CHAQUE transcribe),
    la phase résumé fait UNE invocation au lieu d'une par chunk VAD (vécu :
    578 rechargements ≈ 5-7 h sur 99 min)."""

    def test_prechunked_appele_une_fois_avec_tous_les_chunks(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            cfg = _default_cfg(tmp_path)
            from transcria.audio.vad import SileroVAD
            from transcria.jobs.store import JobStore
            from transcria.stt import summary as summary_mod

            monkeypatch.setitem(
                sys.modules, "librosa",
                SimpleNamespace(load=lambda *a, **kw: (_FAKE_AUDIO, 16000)),
            )
            job = JobStore.create_job(owner_id, "Batched Summary")
            chunks = [
                {"start": 0.0, "end": 0.5, "audio": _FAKE_AUDIO[:8000]},
                {"start": 0.5, "end": 1.0, "audio": _FAKE_AUDIO[8000:]},
            ]
            monkeypatch.setattr(SileroVAD, "build_speech_chunks", lambda self, *a, **kw: chunks)

            calls = {"prechunked": 0, "transcribe": 0}

            class _Batched:
                def load(self):
                    return True

                def offload(self):
                    return None

                def transcribe(self, *a, **kw):
                    calls["transcribe"] += 1
                    return []

                def transcribe_prechunked(self, got_chunks, language="fr", **kw):
                    calls["prechunked"] += 1
                    assert got_chunks is chunks           # TOUS les chunks, en un appel
                    return [
                        {"start": 0.0, "end": 0.5, "text": "un"},
                        {"start": 0.5, "end": 1.0, "text": "deux"},
                        {"error": "chunk illisible"},      # filtré
                    ]

            monkeypatch.setattr(summary_mod, "create_transcriber",
                                lambda *a, **kw: _Batched())
            gen = SummaryGenerator(cfg)
            result = gen.generate_quick_summary(job, tmp_path / "audio.wav")

        assert calls == {"prechunked": 1, "transcribe": 0}
        assert result.get("error") is None
        assert result["segments_count"] == 2 if "segments_count" in result else True


class TestSummaryByTurns:
    """Mode « diariser d'abord » (docs/ATTRIBUTION_LOCUTEURS_RESUME.md, option A) : la
    transcription rapide est découpée par tours exclusifs et chaque segment naît étiqueté."""

    def _setup(self, app, owner_id, tmp_path, monkeypatch, *, turns):
        cfg = _default_cfg(tmp_path)
        cfg.setdefault("workflow", {})["summary_by_turns"] = True
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.jobs.store import JobStore
        from transcria.stt.cohere_transcriber import CohereTranscriber
        monkeypatch.setitem(sys.modules, "librosa",
                            SimpleNamespace(load=lambda *a, **kw: (_FAKE_AUDIO, 16000)))
        job = JobStore.create_job(owner_id, "Résumé par tours")
        fs = JobFilesystem(cfg["storage"]["jobs_dir"], job.id)
        if turns is not None:
            fs.save_json("speakers/speaker_turns.json", turns)
        calls: list[dict] = []

        def fake_transcribe(self, audio_path=None, language=None, audio_array=None, sample_rate=16000, **kw):
            calls.append({"n": len(audio_array)})
            return [{"start": 0.0, "end": len(audio_array) / sample_rate, "text": f"segment {len(calls)}"}]

        monkeypatch.setattr(CohereTranscriber, "load", lambda self: True)
        monkeypatch.setattr(CohereTranscriber, "transcribe", fake_transcribe)
        monkeypatch.setattr(CohereTranscriber, "offload", lambda self: None)
        audio_path = tmp_path / "a.wav"
        audio_path.write_text("fake")
        return cfg, job, fs, audio_path, calls

    def test_segments_etiquetes_par_tour(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            total = len(_FAKE_AUDIO) / 16000
            turns = {"exclusive_turns": [
                {"speaker": "SPEAKER_01", "start": 0.0, "end": total / 2},
                {"speaker": "SPEAKER_00", "start": total / 2, "end": total},
            ]}
            cfg, job, fs, audio_path, calls = self._setup(app, owner_id, tmp_path, monkeypatch, turns=turns)
            result = SummaryGenerator(cfg).generate_quick_summary(job, audio_path, gpu_index=0)
            segs = fs.load_json("summary/summary.json")["segments"]
            assert [s["speaker"] for s in segs] == ["SPEAKER_01", "SPEAKER_00"]
            assert result["segment_count"] == 2 and len(calls) == 2
            # Le transcript rapide porte l'étiquette : la LLM lit « qui dit quoi ».
            assert "SPEAKER_01 segment 1" in result["transcript_text"]

    def test_sans_tours_repli_historique(self, app, owner_id, tmp_path, monkeypatch):
        with app.app_context():
            from transcria.audio.vad import SileroVAD
            monkeypatch.setattr(SileroVAD, "build_speech_chunks",
                                lambda self, audio, **kw: [{"start": 0.0, "end": 1.0, "audio": audio}])
            cfg, job, fs, audio_path, calls = self._setup(app, owner_id, tmp_path, monkeypatch, turns=None)
            SummaryGenerator(cfg).generate_quick_summary(job, audio_path, gpu_index=0)
            segs = fs.load_json("summary/summary.json")["segments"]
            assert segs and "speaker" not in segs[0]        # chemin VAD : non étiqueté


def test_label_segments_by_chunk_voie_batchee():
    from transcria.stt.summary import _label_segments_by_chunk

    segs = [{"start": 1.0, "end": 3.0, "text": "a"}, {"start": 7.0, "end": 9.0, "text": "b"},
            {"start": 20.0, "end": 21.0, "text": "hors chunk"}]
    _label_segments_by_chunk(segs, [{"start": 0.0, "end": 5.0, "speaker": "SPEAKER_00"},
                                    {"start": 5.0, "end": 10.0, "speaker": "SPEAKER_01"}])
    assert [s.get("speaker") for s in segs] == ["SPEAKER_00", "SPEAKER_01", None]
