"""Éditeur SRT — modèle serveur (lot A, docs/EDITEUR_SRT_INTEGRE.md §3.1).

Le TEST D'OR : round-trip parse→serialize à l'octet près (à la normalisation unique
du saut de ligne final près) sur des SRT au format RÉEL du pipeline — l'éditeur
n'altère jamais ce que l'utilisateur n'a pas touché.
"""
import pytest

from transcria.workflow.srt_editor import (
    SrtParseError,
    compute_speaker_stats,
    join_speaker_prefix,
    parse_srt_chunks,
    serialize_chunks,
    split_speaker_prefix,
    validate_chunks,
)

# Reproduit le format réel observé (jobs de dev) : préfixe avec nom, sans nom, absent.
REAL_SHAPE_SRT = (
    "1\n00:00:01,012 --> 00:00:03,910\nSPEAKER_01(Vendeur / fromager): Podcast francefacil.com\n\n"
    "2\n00:00:05,416 --> 00:00:06,762\nSPEAKER_00(Cliente): Fais pas chaud ce matin.\n\n"
    "3\n00:00:07,053 --> 00:00:11,639\nSPEAKER_01: Non, et ils annoncent rien de bon.\n\n"
    "4\n01:02:03,004 --> 01:02:05,999\nTexte sans préfixe de locuteur.\n"
)


class TestRoundTrip:
    def test_or_round_trip_octet(self):
        assert serialize_chunks(parse_srt_chunks(REAL_SHAPE_SRT)) == REAL_SHAPE_SRT

    def test_normalisation_unique_du_saut_final(self):
        sans_lf = REAL_SHAPE_SRT.rstrip("\n")
        assert serialize_chunks(parse_srt_chunks(sans_lf)) == sans_lf + "\n"

    def test_renumerotation_sequentielle(self):
        desordre = REAL_SHAPE_SRT.replace("1\n00:00:01", "7\n00:00:01").replace("2\n00:00:05", "42\n00:00:05")
        rt = serialize_chunks(parse_srt_chunks(desordre))
        assert rt.startswith("1\n") and "\n\n2\n" in rt

    def test_multiligne_conserve(self):
        srt = "1\n00:00:00,000 --> 00:00:02,000\nSPEAKER_00(A): Ligne un.\nLigne deux.\n"
        chunks = parse_srt_chunks(srt)
        assert chunks[0]["text"] == "Ligne un.\nLigne deux."
        assert serialize_chunks(chunks) == srt


class TestParseTolerant:
    def test_champs_extraits(self):
        chunks = parse_srt_chunks(REAL_SHAPE_SRT)
        assert [c["speaker_id"] for c in chunks] == ["SPEAKER_01", "SPEAKER_00", "SPEAKER_01", None]
        assert chunks[0]["speaker_name"] == "Vendeur / fromager"
        assert chunks[2]["speaker_name"] is None
        assert chunks[3]["text"] == "Texte sans préfixe de locuteur."
        assert chunks[0]["start_ms"] == 1012 and chunks[3]["end_ms"] == 3725999

    def test_bloc_malforme_isole_ignore(self):
        srt = REAL_SHAPE_SRT + "\nn'importe quoi sans timestamp\n"
        assert len(parse_srt_chunks(srt)) == 4

    def test_bloc_sans_index_tolere(self):
        srt = "00:00:00,000 --> 00:00:01,000\nSPEAKER_00: Sans index.\n"
        assert parse_srt_chunks(srt)[0]["text"] == "Sans index."

    def test_vide_ok_illisible_erreur(self):
        assert parse_srt_chunks("") == []
        with pytest.raises(SrtParseError):
            parse_srt_chunks("du texte qui n'est pas du SRT du tout")

    def test_prefixe_round_trip(self):
        for sid, name, text in [("SPEAKER_03", "Mme X", "Bonjour."), ("SPEAKER_03", None, "Bonjour."), (None, None, "Bonjour.")]:
            assert split_speaker_prefix(join_speaker_prefix(sid, name, text)) == (sid, name, text)


class TestValidate:
    def test_avertissements_non_bloquants(self):
        chunks = parse_srt_chunks(REAL_SHAPE_SRT)
        chunks[1]["start_ms"] = chunks[0]["end_ms"] - 500     # chevauchement
        chunks[2]["end_ms"] = chunks[2]["start_ms"]           # durée nulle
        chunks[3]["text"] = "  "                              # vide
        warnings = validate_chunks(chunks)
        assert any("Chevauchements : 1" in w for w in warnings)
        assert any("durée nulle" in w for w in warnings)
        assert any("texte vide" in w for w in warnings)

    def test_srt_propre_zero_avertissement(self):
        assert validate_chunks(parse_srt_chunks(REAL_SHAPE_SRT)) == []

    def test_depassement_duree_audio(self):
        warnings = validate_chunks(parse_srt_chunks(REAL_SHAPE_SRT), audio_duration_ms=60000)
        assert any("dépasse la durée" in w for w in warnings)


class TestSpeakerStats:
    def test_recalcul_format_docx(self):
        stats = compute_speaker_stats(parse_srt_chunks(REAL_SHAPE_SRT))
        speakers = {s["speaker_id"]: s for s in stats["speakers"]}
        # SPEAKER_01 : (3910-1012) + (11639-7053) = 7.484 s sur 2 tours
        assert speakers["SPEAKER_01"]["speaking_time_seconds"] == pytest.approx(7.484)
        assert speakers["SPEAKER_01"]["turn_count"] == 2
        assert speakers["SPEAKER_00"]["turn_count"] == 1
        # tri par temps décroissant + source tracée
        assert stats["speakers"][0]["speaker_id"] == "SPEAKER_01"
        assert stats["source"] == "srt_editor"

    def test_chunks_sans_locuteur_agreges(self):
        stats = compute_speaker_stats(parse_srt_chunks(REAL_SHAPE_SRT))
        assert any(s["speaker_id"] == "—" for s in stats["speakers"])


class TestPrefixePistesSeparees:
    """Vague 5 (pistes séparées) : l'identifiant de locuteur EST le nom du participant —
    espaces et accents compris. Bug vécu à l'éditeur (2026-07-30) : « Alice
    Dupont(Alice Dupont): » doublé à l'émission, et « 0 locuteurs » car le parseur
    n'acceptait que SPEAKER_nn. Contrat : forme forte `Id(Nom):` toujours reconnue,
    identifiant LIBRE `Nom: …` reconnu SEULEMENT s'il est CONNU du job — jamais de
    locuteur fantôme depuis une phrase à deux-points."""

    def test_nom_connu_reconnu(self):
        spk, name, rest = split_speaker_prefix(
            "Alice Dupont: C'était où la ferme ?", known_ids={"Alice Dupont"})
        assert (spk, name, rest) == ("Alice Dupont", None, "C'était où la ferme ?")

    def test_phrase_a_deux_points_jamais_un_locuteur(self):
        spk, name, rest = split_speaker_prefix(
            "Attention: il pleut", known_ids={"Alice Dupont"})
        assert spk is None and rest == "Attention: il pleut"

    def test_forme_forte_reconnue_meme_inconnue(self):
        spk, name, _ = split_speaker_prefix("Alice Dupont(Le Président): Bonjour")
        assert (spk, name) == ("Alice Dupont", "Le Président")

    def test_piste_anonyme_motif_machine(self):
        spk, _, _ = split_speaker_prefix("PISTE_e369f088: Bonjour")
        assert spk == "PISTE_e369f088"

    def test_join_ne_double_jamais_le_nom(self):
        assert join_speaker_prefix("Alice Dupont", "Alice Dupont", "Bonjour") \
            == "Alice Dupont: Bonjour"
        assert join_speaker_prefix("SPEAKER_01", "Alice", "Bonjour") \
            == "SPEAKER_01(Alice): Bonjour"

    def test_round_trip_srt_par_piste(self):
        srt = ("1\n00:00:00,000 --> 00:00:12,000\nAlice Dupont: Asie.\n\n"
               "2\n00:00:00,160 --> 00:00:30,160\nBob Morane: Et après, on avait soif.\n")
        chunks = parse_srt_chunks(srt, known_speaker_ids={"Alice Dupont", "Bob Morane"})
        assert [c["speaker_id"] for c in chunks] == ["Alice Dupont", "Bob Morane"]
        assert serialize_chunks(chunks).strip() == srt.strip()


class TestEmissionSansDoublon:
    def test_segments_to_srt_id_egal_nom(self):
        from transcria.stt.base_transcriber import BaseTranscriber

        class _T:                                   # duck-type minimal, sans ABC
            segments_to_srt = BaseTranscriber.segments_to_srt
            _seconds_to_srt_time = staticmethod(BaseTranscriber._seconds_to_srt_time)

        srt = _T().segments_to_srt(
            [{"start": 0.0, "end": 2.0, "text": "Bonjour", "speaker": "Alice Dupont"}],
            {"Alice Dupont": {"name": "Alice Dupont"}})
        assert "Alice Dupont: Bonjour" in srt
        assert "Alice Dupont(Alice Dupont)" not in srt
