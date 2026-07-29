"""Vague 2 (plan UI_REUNIONS §6.3/D5) — manifeste participants : validation PURE + projection.

Ce que ces tests verrouillent : (1) la validation STRICTE (un manifeste douteux est rejeté en
bloc — de fausses suggestions de noms seraient validées par habitude à l'étape 5) ; (2) la
projection fenêtres × diarisation (solo net → suggestion, ambigu → rien, salle → regroupement
sans nom) ; (3) les semis (participants.json, speaker_hint)."""
from __future__ import annotations

from transcria.ingestion.manifest import (
    parse_participants_manifest,
    seed_participants,
    speaker_hint_from_manifest,
)
from transcria.workflow.speaker_manifest import project_speakers


def _manifest(participants):
    return {"version": 1, "source": "zoom-sdk", "mix": "timeline_common",
            "participants": participants}


SOLO_A = {"id": "p1", "name": "Alice Durand", "kind": "solo",
          "speech_windows": [[0.0, 10.0], [20.0, 30.0]]}
SOLO_B = {"id": "p2", "name": "Benoît Marchand", "kind": "solo",
          "speech_windows": [[10.0, 20.0]]}
ROOM = {"id": "p3", "name": "Salle Marengo", "kind": "room",
        "speech_windows": [[30.0, 60.0]]}


# ── Validation stricte ────────────────────────────────────────────────────────

class TestParse:
    def test_manifeste_valide(self):
        m, err = parse_participants_manifest(_manifest([SOLO_A, ROOM]))
        assert err == "" and m is not None
        assert [p.id for p in m.solo_participants] == ["p1"]
        assert [p.id for p in m.room_participants] == ["p3"]
        assert m.participants[0].speech_total_s == 20.0

    def test_unknown_est_traite_en_room(self):
        m, _ = parse_participants_manifest(_manifest([dict(SOLO_A, kind="unknown")]))
        assert m.participants[0].is_solo is False       # prudence : piste ≠ personne

    def test_rejets_en_bloc(self):
        cas = [
            ({"version": 2, "participants": [SOLO_A]}, "version"),
            (_manifest([]), "vide"),
            (_manifest([dict(SOLO_A, id="")]), "id"),
            (_manifest([SOLO_A, dict(SOLO_B, id="p1")]), "dupliqué"),
            (_manifest([dict(SOLO_A, kind="humain")]), "kind"),
            (_manifest([dict(SOLO_A, speech_windows=[[5.0, 2.0]])]), "incohérente"),
            (_manifest([dict(SOLO_A, speech_windows=[["x", 2.0]])]), "numérique"),
            (_manifest([dict(SOLO_A, speech_windows="plein")]), "absent"),
            ("pas un objet", "objet"),
        ]
        for raw, fragment in cas:
            m, err = parse_participants_manifest(raw)
            assert m is None and fragment in err, (raw, err)

    def test_fenetres_triees_a_la_lecture(self):
        m, _ = parse_participants_manifest(
            _manifest([dict(SOLO_A, speech_windows=[[20.0, 30.0], [0.0, 10.0]])]))
        assert m.participants[0].speech_windows == ((0.0, 10.0), (20.0, 30.0))


# ── Semis ─────────────────────────────────────────────────────────────────────

class TestSeeds:
    def test_speaker_hint_compte_les_salles_en_plus(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, SOLO_B, ROOM]))
        assert speaker_hint_from_manifest(m) == {"min": 3, "max": 6}

    def test_seed_participants_exclut_les_salles(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, ROOM]))
        seeded = seed_participants(m)
        assert [p["name"] for p in seeded] == ["Alice Durand"]   # un lieu n'est pas un participant

    def test_seed_ignore_les_solos_sans_nom(self):
        m, _ = parse_participants_manifest(_manifest([dict(SOLO_A, name="")]))
        assert seed_participants(m) == []


# ── Projection ────────────────────────────────────────────────────────────────

def _turns(*spans):
    return [{"speaker": s, "start": a, "end": b} for s, a, b in spans]


class TestProjection:
    def test_solo_net_suggere_le_nom(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, SOLO_B]))
        result = project_speakers(m, _turns(("SPEAKER_00", 1.0, 9.0), ("SPEAKER_01", 11.0, 19.0)))
        assert result.suggestion_for("SPEAKER_00").name == "Alice Durand"
        assert result.suggestion_for("SPEAKER_01").name == "Benoît Marchand"
        assert result.rooms == {}

    def test_ambigu_sous_la_marge_ne_suggere_rien(self):
        # SPEAKER à cheval 50/50 entre deux pistes solo : marge nulle → aucune suggestion.
        m, _ = parse_participants_manifest(_manifest([SOLO_A, SOLO_B]))
        result = project_speakers(m, _turns(("SPEAKER_00", 5.0, 15.0)))
        assert result.suggestions == ()
        assert "SPEAKER_00" in result.scores       # mais l'audit garde les scores

    def test_salle_regroupe_sans_nommer(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, ROOM]))
        result = project_speakers(
            m, _turns(("SPEAKER_02", 31.0, 40.0), ("SPEAKER_04", 45.0, 59.0)))
        assert result.suggestions == ()            # jamais de nom depuis un micro de salle
        assert result.rooms == {"Salle Marengo": ("SPEAKER_02", "SPEAKER_04")}

    def test_sous_le_seuil_de_recouvrement_rien(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A]))
        # 4 s sur 10 s de parole recouvrent la piste → ratio 0,4 < 0,65.
        result = project_speakers(m, _turns(("SPEAKER_00", 6.0, 16.0)))
        assert result.suggestions == () and result.rooms == {}

    def test_tours_malformes_ignores_sans_crash(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A]))
        turns = [{"speaker": "SPEAKER_00", "start": "x", "end": 2},
                 {"start": 1, "end": 2},
                 {"speaker": "SPEAKER_00", "start": 1.0, "end": 9.0}]
        assert project_speakers(m, turns).suggestion_for("SPEAKER_00") is not None

    def test_seuils_injectables(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, SOLO_B]))
        strict = project_speakers(m, _turns(("SPEAKER_00", 5.0, 15.0)),
                                  min_overlap_ratio=0.4, min_margin=0.0)
        assert strict.suggestion_for("SPEAKER_00") is not None

    def test_audit_dict_complet(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A]))
        result = project_speakers(m, _turns(("SPEAKER_00", 1.0, 9.0)))
        audit = result.to_audit_dict(min_overlap_ratio=0.65, min_margin=0.2)
        assert audit["thresholds"]["min_overlap_ratio"] == 0.65
        assert audit["suggestions"][0]["speaker"] == "SPEAKER_00"
        assert "SPEAKER_00" in audit["scores"]


class TestSoloTrackActuallyShared:
    """Durcissement (constat utilisateur 2026-07-29) : une piste déclarée `solo` peut cacher
    plusieurs personnes — si la diarisation y voit DEUX voix, plus aucune suggestion de nom,
    la piste s'affiche comme un micro partagé."""

    def test_deux_voix_sur_une_piste_solo_aucun_nom_suggere(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A]))
        # Deux SPEAKER distincts, chacun majoritairement sur la piste d'Alice.
        result = project_speakers(m, _turns(("SPEAKER_00", 0.0, 8.0), ("SPEAKER_01", 20.0, 28.0)))
        assert result.suggestions == ()                          # jamais le même nom pour deux voix
        assert result.rooms == {"Alice Durand": ("SPEAKER_00", "SPEAKER_01")}

    def test_une_seule_voix_reste_suggeree(self):
        m, _ = parse_participants_manifest(_manifest([SOLO_A, SOLO_B]))
        result = project_speakers(m, _turns(("SPEAKER_00", 1.0, 9.0), ("SPEAKER_01", 11.0, 19.0)))
        assert {s.name for s in result.suggestions} == {"Alice Durand", "Benoît Marchand"}


class TestTurnsFromManifest:
    """« Perdre l'avantage des locuteurs fait perdre beaucoup trop » (utilisateur,
    2026-07-29) : les tours viennent des PISTES — exacts même en parole simultanée,
    jamais de sur-découpage d'une voix unique."""

    def test_tours_exacts_et_noms_affichables(self):
        from transcria.ingestion.manifest_turns import turns_from_manifest
        m, _ = parse_participants_manifest(_manifest([SOLO_A, ROOM]))
        r = turns_from_manifest(m)
        assert r["available"] and r["source"] == "manifest"
        assert r["speakers"] == ["Alice Durand", "Salle Marengo"]   # noms, pas SPEAKER_XX
        assert r["stats"]["Alice Durand"]["turn_count"] == 2
        assert r["turns"][0]["start"] == 0.0

    def test_parole_simultanee_conservee(self):
        # Deux pistes qui parlent EN MÊME TEMPS : deux tours qui se chevauchent — l'avantage
        # que le mixage seul perdait.
        from transcria.ingestion.manifest_turns import turns_from_manifest
        a = dict(SOLO_A, speech_windows=[[0.0, 10.0]])
        b = dict(SOLO_B, speech_windows=[[5.0, 15.0]])
        r = turns_from_manifest(parse_participants_manifest(_manifest([a, b]))[0])
        assert [(t["speaker"], t["start"], t["end"]) for t in r["turns"]] == [
            ("Alice Durand", 0.0, 10.0), ("Benoît Marchand", 5.0, 15.0)]

    def test_piste_sans_nom_id_lisible(self):
        from transcria.ingestion.manifest_turns import turns_from_manifest
        m, _ = parse_participants_manifest(_manifest([dict(SOLO_A, name="")]))
        assert turns_from_manifest(m)["speakers"] == ["PISTE_p1"]
