"""Réutilisation du préflight de la phase analyze dans le pipeline (PISTES_AMELIORATION §2.5).

Sortie-équivalent : le pipeline recharge `metadata/audio_preflight.json` si son
empreinte source (chemin+taille+mtime) correspond au fichier audio courant, au
lieu de refaire décodage + SQUIM + DNSMOS. `reuse_analysis: false` = recalcul
systématique (comportement d'avant 0.3.8).
"""
from __future__ import annotations

from types import SimpleNamespace

from builders import make_job_stub

from transcria.audio.preflight import source_fingerprint
from transcria.jobs.filesystem import JobFilesystem
from transcria.services.pipeline_steps import preflight as preflight_step


class _FakeAnalyzer:
    """Substitut d'AudioPreflightAnalyzer : compte les calculs réels."""

    calls = 0

    def __init__(self, config):
        self.enabled = True
        cfg = config.get("workflow", {}).get("audio_preflight", {}) or {}
        self.reuse_analysis = bool(cfg.get("reuse_analysis", True))

    def analyze(self, path):
        type(self).calls += 1
        return {"rms": 0.05, "risk_level": "ok", "source_fingerprint": source_fingerprint(path)}


class _Sl:
    def info(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _svc(tmp_path, *, reuse=True):
    config = {
        "storage": {"jobs_dir": str(tmp_path / "jobs")},
        "workflow": {"audio_preflight": {"enabled": True, "reuse_analysis": reuse}},
    }
    progress = SimpleNamespace(update=lambda *a, **k: None)
    return SimpleNamespace(config=config, progress=progress)


def _setup(tmp_path, monkeypatch):
    monkeypatch.setattr(preflight_step, "AudioPreflightAnalyzer", _FakeAnalyzer)
    _FakeAnalyzer.calls = 0
    job = make_job_stub()
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"RIFFxxxxWAVE" * 10)
    return job, audio


class TestPreflightReuse:
    def test_reutilise_le_json_de_la_phase_analyze(self, tmp_path, monkeypatch):
        job, audio = _setup(tmp_path, monkeypatch)
        svc = _svc(tmp_path)
        # La phase analyze a déjà calculé et sauvegardé le préflight de CE fichier.
        fs = JobFilesystem(svc.config["storage"]["jobs_dir"], job.id)
        fs.save_json("metadata/audio_preflight.json",
                     {"rms": 0.042, "risk_level": "suspect",
                      "source_fingerprint": source_fingerprint(audio)})

        result = preflight_step.run(svc, job, str(audio), _Sl())

        assert result["rms"] == 0.042  # le JSON stocké EST le résultat
        assert _FakeAnalyzer.calls == 0  # aucun recalcul

    def test_recalcule_si_reuse_analysis_false(self, tmp_path, monkeypatch):
        job, audio = _setup(tmp_path, monkeypatch)
        svc = _svc(tmp_path, reuse=False)
        fs = JobFilesystem(svc.config["storage"]["jobs_dir"], job.id)
        fs.save_json("metadata/audio_preflight.json",
                     {"rms": 0.042, "source_fingerprint": source_fingerprint(audio)})

        result = preflight_step.run(svc, job, str(audio), _Sl())

        assert _FakeAnalyzer.calls == 1
        assert result["rms"] == 0.05  # résultat frais, pas le JSON stocké

    def test_recalcule_si_l_audio_a_change(self, tmp_path, monkeypatch):
        job, audio = _setup(tmp_path, monkeypatch)
        svc = _svc(tmp_path)
        fs = JobFilesystem(svc.config["storage"]["jobs_dir"], job.id)
        fs.save_json("metadata/audio_preflight.json",
                     {"rms": 0.042, "source_fingerprint": "empreinte-d-un-autre-fichier"})

        preflight_step.run(svc, job, str(audio), _Sl())

        assert _FakeAnalyzer.calls == 1  # fichier remplacé → recalcul obligatoire

    def test_recalcule_sans_json_stocke(self, tmp_path, monkeypatch):
        job, audio = _setup(tmp_path, monkeypatch)
        svc = _svc(tmp_path)

        result = preflight_step.run(svc, job, str(audio), _Sl())

        assert _FakeAnalyzer.calls == 1
        # le résultat frais est sauvegardé AVEC son empreinte : le prochain
        # passage (reprise, re-soumission du même audio) le réutilisera.
        fs = JobFilesystem(svc.config["storage"]["jobs_dir"], job.id)
        stored = fs.load_json("metadata/audio_preflight.json")
        assert stored["source_fingerprint"] == source_fingerprint(audio)
        assert result["source_fingerprint"] == stored["source_fingerprint"]

    def test_json_ancien_sans_empreinte_declenche_recalcul(self, tmp_path, monkeypatch):
        # Jobs d'avant 0.3.8 : audio_preflight.json sans source_fingerprint.
        job, audio = _setup(tmp_path, monkeypatch)
        svc = _svc(tmp_path)
        fs = JobFilesystem(svc.config["storage"]["jobs_dir"], job.id)
        fs.save_json("metadata/audio_preflight.json", {"rms": 0.042})

        preflight_step.run(svc, job, str(audio), _Sl())

        assert _FakeAnalyzer.calls == 1
