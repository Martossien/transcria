"""Anti-hallucination par moteur : catalogue, scorer sensible au backend, politique
de suppression corroborée (étage A) et sa règle d'or — jamais sur un signal isolé.
"""
from __future__ import annotations

import pytest

from transcria.stt import hallucination_catalog as catalog
from transcria.stt.hallucination_policy import apply_deletion_policy, deletion_enabled
from transcria.stt.reliability import SegmentReliabilityScorer


@pytest.fixture(autouse=True)
def _clear_catalog_caches():
    def clear():
        # _load_raw peut être substitué par un lambda sans cache (monkeypatch encore
        # actif au teardown) — on ne purge que ce qui est réellement caché.
        for fn in (catalog._load_raw, catalog.signatures_for_backend):
            cache_clear = getattr(fn, "cache_clear", None)
            if cache_clear:
                cache_clear()
    clear()
    yield
    clear()


def _fake_catalog(monkeypatch, data: dict):
    monkeypatch.setattr(catalog, "_load_raw", lambda: data)
    catalog.signatures_for_backend.cache_clear()


class TestCatalog:
    def test_le_catalogue_versionne_charge_whisper_et_generic(self):
        # Le VRAI fichier data/ : whisper hérite du générique + ses signatures propres.
        whisper = catalog.signatures_for_backend("whisper")
        generic = catalog.signatures_for_backend("kroko")  # pas de section kroko → générique seul
        assert len(whisper) > len(generic) > 0
        assert all(s.action in ("flag", "delete") for s in whisper)

    def test_amara_est_une_signature_delete_whisper(self):
        matched = catalog.match_signature(
            "Sous-titres réalisés par la communauté d'Amara.org", "whisper")
        assert matched is not None and matched.action == "delete"
        # Un moteur SANS cette signature ne la voit pas (elle est propre à whisper).
        assert catalog.match_signature(
            "Sous-titres réalisés par la communauté d'Amara.org", "cohere") is None

    def test_credit_nominatif_whisper_delete_mais_pas_le_propos_reel(self):
        # Minage 2026-08-05 : « Sous-titres par <nom inventé> » sur audio inaudible.
        matched = catalog.match_signature("Sous-titres par Jérémy Diaz", "whisper")
        assert matched is not None and matched.action == "delete"
        # Une phrase réelle qui PARLE de sous-titres n'est pas un crédit.
        assert catalog.match_signature(
            "Alors les sous-titres par défaut, il faudra les activer pour la salle",
            "whisper") is None

    def test_voxtral_formules_de_politesse_flag_seulement(self):
        # Minage 2026-08-05 (bobines non-parole) : 3 sources indépendantes.
        matched = catalog.match_signature("Je ne sais pas.", "voxtral")
        assert matched is not None and matched.action == "flag"
        assert catalog.match_signature("Je suis désolé.", "voxtral").action == "flag"
        # Segment entier uniquement : la vraie phrase qui continue est indemne.
        assert catalog.match_signature(
            "Je ne sais pas si on aura le budget au T3", "voxtral") is None
        # Signature propre à voxtral : un autre moteur ne la voit pas.
        assert catalog.match_signature("Je ne sais pas.", "whisper") is None

    def test_cohere_tic_appris_flag_avec_variante_degeneree(self):
        # Minage 2026-08-05 : émis à l'identique sur zones muettes, 2 sources.
        assert catalog.match_signature("C'est un peu comme ça.", "cohere").action == "flag"
        assert catalog.match_signature(
            "Enfin, en fait, c'est un peu comme ça.", "cohere").action == "flag"
        assert catalog.match_signature(
            "C'est un peu comme ça qu'on procède d'habitude", "cohere") is None

    def test_entrees_invalides_ignorees_sans_casser(self, monkeypatch):
        _fake_catalog(monkeypatch, {"generic": [
            {"pattern": "[regex cassée", "action": "delete"},
            {"pattern": "\\bvalide\\b", "action": "inconnue"},
            {"pattern": "\\bgardée\\b", "action": "flag"},
            "pas un dict",
        ]})
        sigs = catalog.signatures_for_backend(None)
        assert len(sigs) == 1 and sigs[0].regex.pattern == "\\bgardée\\b"

    def test_delete_prioritaire_sur_flag(self, monkeypatch):
        _fake_catalog(monkeypatch, {"generic": [
            {"pattern": "merci", "action": "flag"},
            {"pattern": "merci d['’]avoir regardé", "action": "delete"},
        ]})
        assert catalog.match_signature("Merci d'avoir regardé", None).action == "delete"


class TestScorerParMoteur:
    def test_signature_moteur_ajoute_raison_et_dossier(self):
        segments = [{"start": 10.0, "end": 12.0,
                     "text": "Sous-titres réalisés par la communauté d'Amara.org"}]
        scored = SegmentReliabilityScorer({}).score_segments(segments, backend="whisper")
        assert "signature_hallucination_moteur" in scored[0]["reliability_reasons"]
        assert scored[0]["reliability"] == "degrade"  # flag textuel fort
        sig = scored[0]["hallucination_signature"]
        assert sig["action"] == "delete" and sig["source"] == "whisper"

    def test_sans_backend_seul_le_generique_s_applique(self):
        segments = [{"start": 10.0, "end": 12.0,
                     "text": "Sous-titres réalisés par la communauté d'Amara.org"}]
        scored = SegmentReliabilityScorer({}).score_segments(segments, backend=None)
        assert "signature_hallucination_moteur" not in scored[0]["reliability_reasons"]

    def test_texte_normal_indemne(self):
        segments = [{"start": 0.0, "end": 6.0,
                     "text": "Nous validons le budget du deuxième trimestre avec l'équipe."}]
        scored = SegmentReliabilityScorer({}).score_segments(segments, backend="whisper")
        assert scored[0]["reliability"] == "ok"
        assert "hallucination_signature" not in scored[0]


class TestPolitiqueSuppression:
    def _segment_delete(self, **extra):
        return {"start": 10.0, "end": 14.0, "speaker": "SPEAKER_00",
                "text": "Merci d'avoir regardé",
                "hallucination_signature": {"pattern": "p", "action": "delete",
                                            "source": "whisper"}, **extra}

    def test_signature_seule_ne_supprime_jamais(self):
        # LA règle d'or : un signal isolé (même une signature delete) ne suffit pas.
        kept, removed = apply_deletion_policy(
            [self._segment_delete()], scene={"scene_segments": []}, config={})
        assert removed == [] and len(kept) == 1

    def test_corroboration_no_speech_prob(self):
        kept, removed = apply_deletion_policy(
            [self._segment_delete(no_speech_prob=0.93)], scene=None, config={})
        assert len(removed) == 1 and kept == []
        assert removed[0]["corroboration"].startswith("no_speech_prob=0.93")
        assert removed[0]["text"] == "Merci d'avoir regardé"
        assert removed[0]["signature_source"] == "whisper"

    def test_no_speech_prob_de_signalement_ne_suffit_pas(self):
        # 0.6 déclenche le SIGNALEMENT (seuil 0.5) mais pas la SUPPRESSION (seuil 0.8) :
        # supprimer est plus grave que signaler, le seuil est volontairement plus dur.
        kept, removed = apply_deletion_policy(
            [self._segment_delete(no_speech_prob=0.6)], scene=None, config={})
        assert removed == [] and len(kept) == 1

    def test_corroboration_recouvrement_musique_et_silence(self):
        scene = {"scene_segments": [
            {"label": "music", "start": 9.0, "end": 12.5},
            {"label": "noEnergy", "start": 12.5, "end": 13.5},
            {"label": "male", "start": 13.5, "end": 20.0},
        ]}
        kept, removed = apply_deletion_policy(
            [self._segment_delete()], scene=scene, config={})
        assert len(removed) == 1
        assert "recouvrement_non_parole" in removed[0]["corroboration"]

    def test_recouvrement_minoritaire_ne_suffit_pas(self):
        scene = {"scene_segments": [{"label": "music", "start": 10.0, "end": 11.0},
                                    {"label": "female", "start": 11.0, "end": 14.0}]}
        kept, removed = apply_deletion_policy(
            [self._segment_delete()], scene=scene, config={})
        assert removed == [] and len(kept) == 1

    def test_action_flag_jamais_supprimee_meme_corroboree(self):
        segment = self._segment_delete(no_speech_prob=0.99)
        segment["hallucination_signature"] = {"pattern": "p", "action": "flag",
                                              "source": "generic"}
        kept, removed = apply_deletion_policy([segment], scene=None, config={})
        assert removed == [] and len(kept) == 1

    def test_desactivable_par_configuration(self):
        config = {"workflow": {"segment_reliability":
                               {"delete_confirmed_hallucinations": False}}}
        assert deletion_enabled(config) is False
        kept, removed = apply_deletion_policy(
            [self._segment_delete(no_speech_prob=0.99)], scene=None, config=config)
        assert removed == [] and len(kept) == 1

    def test_segments_sans_signature_indemnes(self):
        segments = [{"start": 0.0, "end": 5.0, "text": "Point budget validé.",
                     "no_speech_prob": 0.99}]
        kept, removed = apply_deletion_policy(segments, scene=None, config={})
        assert removed == [] and kept == segments


class TestCoutureTranscription:
    def test_le_pipeline_trace_les_suppressions_sur_disque(self, tmp_path):
        # La couture Transcriber._remove_confirmed_hallucinations : segments filtrés
        # ET dossier de preuves écrit là où le rapport qualité le lit.
        from transcria.jobs.filesystem import JobFilesystem
        from transcria.stt.transcription import Transcriber

        pipeline = Transcriber.__new__(Transcriber)  # sans charger de modèle STT
        pipeline.config = {}
        fs = JobFilesystem(str(tmp_path), "job-hallu")
        fs.save_json("metadata/audio_scene.json", {"scene_segments": [
            {"label": "music", "start": 9.0, "end": 15.0}]})

        class _Sl:
            def info(self, *a, **kw): pass

        segments = [
            {"start": 0.0, "end": 5.0, "text": "Bonjour à tous."},
            {"start": 10.0, "end": 12.0, "text": "Merci d'avoir regardé",
             "hallucination_signature": {"pattern": "p", "action": "delete",
                                         "source": "whisper"}},
        ]
        kept = pipeline._remove_confirmed_hallucinations(segments, fs, _Sl())
        assert [s["text"] for s in kept] == ["Bonjour à tous."]
        removed = fs.load_json("metadata/removed_hallucinations.json")
        assert len(removed) == 1 and removed[0]["text"] == "Merci d'avoir regardé"
        assert "recouvrement_non_parole" in removed[0]["corroboration"]
