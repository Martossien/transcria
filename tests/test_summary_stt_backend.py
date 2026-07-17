"""Backend STT dédié à la phase résumé (PISTES_AMELIORATION §2.1, lot 2).

`models.summary_stt_backend` : null (défaut) = backend principal ; sinon un
moteur rapide dédié au résumé (ex. kroko, CPU pur → zéro réservation VRAM).
Le point de résolution est UNIQUE (`transcriber_factory.summary_backend`) et
consommé par la génération rapide, la réservation VRAM de la phase et la
détection « phase servie à distance ».
"""
from __future__ import annotations

from transcria.config.config_schema import validate_config
from transcria.config.loader import get_default_config
from transcria.stt.transcriber_factory import get_backend_vram_mb, summary_backend
from transcria.workflow.gpu_phase import GpuPhaseSession


class TestResolution:
    def test_null_retombe_sur_le_backend_principal(self):
        cfg = {"models": {"stt_backend": "whisper", "summary_stt_backend": None}}
        assert summary_backend(cfg) == "whisper"

    def test_absent_retombe_sur_le_backend_principal(self):
        assert summary_backend({"models": {"stt_backend": "moss"}}) == "moss"

    def test_defini_prime_sur_le_principal(self):
        cfg = {"models": {"stt_backend": "cohere", "summary_stt_backend": "kroko"}}
        assert summary_backend(cfg) == "kroko"

    def test_defauts_du_loader(self):
        cfg = get_default_config()
        assert cfg["models"]["summary_stt_backend"] is None
        assert summary_backend(cfg) == cfg["models"]["stt_backend"]

    def test_kroko_au_resume_ne_reserve_aucune_vram(self):
        cfg = {"models": {"stt_backend": "cohere", "summary_stt_backend": "kroko"},
               "gpu": {}}
        assert get_backend_vram_mb(summary_backend(cfg), cfg) == 0
        # le pipeline principal garde sa réservation cohere
        assert get_backend_vram_mb(cfg["models"]["stt_backend"], cfg) > 0


class TestSchema:
    def test_null_est_valide(self):
        assert validate_config(get_default_config()).is_valid

    def test_backend_natif_est_valide(self):
        cfg = get_default_config()
        cfg["models"]["summary_stt_backend"] = "kroko"
        assert validate_config(cfg).is_valid

    def test_backend_inconnu_est_refuse(self):
        cfg = get_default_config()
        cfg["models"]["summary_stt_backend"] = "moteur-inexistant"
        result = validate_config(cfg)
        assert not result.is_valid
        assert any("summary_stt_backend" in e for e in result.errors)

    def test_backend_servi_route_est_valide(self):
        cfg = get_default_config()
        cfg["models"]["summary_stt_backend"] = "nemotron"
        cfg.setdefault("inference", {}).setdefault("stt", {})["backends"] = {
            "nemotron": {"url": "http://127.0.0.1:8021/v1"}
        }
        assert validate_config(cfg).is_valid

    def test_backend_servi_non_route_est_refuse(self):
        cfg = get_default_config()
        cfg["models"]["summary_stt_backend"] = "nemotron"
        result = validate_config(cfg)
        assert not result.is_valid


class TestPhaseRemoteResolution:
    """La « distance » de summary_stt se juge sur le backend du RÉSUMÉ."""

    def _gpu(self, cfg):
        session = GpuPhaseSession.__new__(GpuPhaseSession)
        session.config = cfg
        return session

    def test_resume_local_kroko_avec_pipeline_distant(self):
        cfg = {
            "models": {"stt_backend": "cohere", "summary_stt_backend": "kroko"},
            "inference": {"mode": "remote",
                          "stt": {"backends": {"cohere": {"url": "http://node:8002/v1"}}}},
        }
        gpu = self._gpu(cfg)
        assert gpu.phase_runs_remotely("stt") is True        # pipeline → nœud distant
        assert gpu.phase_runs_remotely("summary_stt") is False  # résumé → kroko local

    def test_resume_servi_avec_pipeline_local(self):
        cfg = {
            "models": {"stt_backend": "cohere", "summary_stt_backend": "nemotron"},
            "inference": {"mode": "hybrid",
                          "stt": {"backends": {"nemotron": {"url": "http://127.0.0.1:8021/v1"}}}},
        }
        gpu = self._gpu(cfg)
        assert gpu.phase_runs_remotely("stt") is False
        assert gpu.phase_runs_remotely("summary_stt") is True

    def test_sans_cle_dediee_comportement_historique(self):
        cfg = {
            "models": {"stt_backend": "cohere"},
            "inference": {"mode": "remote",
                          "stt": {"backends": {"cohere": {"url": "http://node:8002/v1"}}}},
        }
        gpu = self._gpu(cfg)
        assert gpu.phase_runs_remotely("stt") is True
        assert gpu.phase_runs_remotely("summary_stt") is True


class TestDiagnosticsSegmentsCourts:
    """Critère « segments courts » RELATIF (lot 2) : détecter les confettis
    d'hallucination sans punir la segmentation d'un moteur streaming.

    Calibré sur le bench réel (4 réunions) : kroko sain ≈ 15 % de segments < 1 s,
    cohere < 3 %, une rafale d'hallucinations > 20 %."""

    @staticmethod
    def _segments(total: int, courts: int) -> list[dict]:
        longs = [{"start": i * 10.0, "end": i * 10.0 + 5.0, "text": "phrase normale"}
                 for i in range(total - courts)]
        petits = [{"start": 1000 + i, "end": 1000 + i + 0.3, "text": "euh"}
                  for i in range(courts)]
        return longs + petits

    def test_rafale_de_confettis_toujours_degradee(self):
        from transcria.stt.summary import SummaryGenerator

        d = SummaryGenerator._build_diagnostics(self._segments(100, 40), 0.7)
        assert "segments_courts_nombreux" in d["flags"]
        assert d["level"] == "degrade"

    def test_style_streaming_sain_non_puni(self):
        from transcria.stt.summary import SummaryGenerator

        # ~15 % de segments courts sur 500 (profil kroko mesuré au bench) : sain.
        d = SummaryGenerator._build_diagnostics(self._segments(500, 75), 0.7)
        assert "segments_courts_nombreux" not in d["flags"]

    def test_petit_nombre_absolu_jamais_flagge(self):
        from transcria.stt.summary import SummaryGenerator

        # 10 courts sur 20 (50 %) : ratio élevé mais volume trop faible pour conclure.
        d = SummaryGenerator._build_diagnostics(self._segments(20, 10), 0.7)
        assert "segments_courts_nombreux" not in d["flags"]
