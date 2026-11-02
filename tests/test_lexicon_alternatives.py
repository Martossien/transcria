"""Formes « A / B » tranchées et mentions séparées des variantes — à l'entrée des termes
douteux dans le job (mesure 2026-11-02 : 458/1305 entrées validées à forme « A / B »,
634/753 « non résolues » venaient de là, 119 autres étaient des mentions légitimes).

Pur : aucun Flask, aucun fichier sauf le test de LexiconManager.save (tmp_path).
"""
from __future__ import annotations

import json

from transcria.context.central_lexicon_service import (
    filter_lexicon_by_srt_presence,
    merge_lexicon_entries,
    prefilter_lexicon_entries_for_display,
)
from transcria.llm_tools.llm_parsing import (
    classify_variant,
    normalize_suspect_terms,
    parse_summary_term_line,
    resolve_alternatives,
    split_variants_and_mentions,
)
from transcria.workflow.speaker_projection import merge_llm_suggestions

TRANSCRIPT = (
    "On passe sur Power B pour le reporting, puis Zabeo pour les tickets. "
    "Le MSSI et le DPO ont validé. Le PEI 25 est clos, on prépare le PEI 26. "
    "Le semestre 2 commence ; Vésinat sera déployé."
)


class TestClassifyVariant:
    def test_graphies_fautives_sont_des_variantes(self):
        assert classify_variant("CEPAM", "cepan") == "variant"
        assert classify_variant("Calicane", "Calicamme") == "variant"
        assert classify_variant("Zabéo", "Zabeo") == "variant"          # accents
        assert classify_variant("NetForm", "net-form") == "variant"       # tiret / casse
        assert classify_variant("Nexalink", "next alink") == "variant"

    def test_mentions_legitimes_ne_sont_pas_des_variantes(self):
        assert classify_variant("PEI", "PEI 25") == "mention"           # forme + mots en plus
        assert classify_variant("Aloha V2", "ALOHA") == "mention"       # variante plus courte que la forme
        assert classify_variant("S2", "semestre 2") == "mention"
        assert classify_variant("AG", "administration générale") == "mention"
        assert classify_variant("SMGT", "système national de gestion du temps") == "mention"
        assert classify_variant("AVES", "Vésinat") == "mention"         # trop éloigné pour être sûr

    def test_vides(self):
        assert classify_variant("", "x") == "mention" and classify_variant("x", "") == "mention"


def test_split_variants_and_mentions_ordre_et_doublons():
    v, m = split_variants_and_mentions("PEI", ["pei", "PEI 25", "P.E.I", "PEI 25", "Pei26"])
    assert v == ["P.E.I", "Pei26"] and m == ["PEI 25"]   # « pei » = forme (exclue), doublon retiré


class TestResolveAlternatives:
    def test_alternatives_identiques_a_la_casse_pres(self):
        out = resolve_alternatives({"term": "Netform / NetForm", "variants": ["net-form"], "comment": "c"}, "")
        assert len(out) == 1 and out[0]["term"] == "Netform" and out[0]["variants"] == ["net-form"]
        assert out[0]["comment"].startswith("Forme retenue « Netform »") and out[0]["comment"].endswith("; c")

    def test_sans_slash_entoure_inchange(self):
        e = {"term": "support 24/7", "variants": ["24-7"], "category": "x"}
        assert resolve_alternatives(e, TRANSCRIPT) == [e]

    def test_graphies_d_un_meme_mot_tranchees_par_la_transcription(self):
        """« Power BI / Power B » : seule « Power BI » est absente du brut → c'est la forme."""
        out = resolve_alternatives({"term": "Power BI / Power B", "variants": ["power bi"], "category": "application",
                                    "priority": "importante", "contexts": [{"text": "On passe sur Power B"}]}, TRANSCRIPT)
        assert len(out) == 1
        e = out[0]
        assert e["term"] == "Power BI" and e["variants"] == ["Power B"] and e["mentions"] == []
        assert e["category"] == "application" and e["priority"] == "importante"
        assert "transcription" in e["comment"] and "Power BI / Power B" in e["comment"]
        assert e["contexts"] == [{"text": "On passe sur Power B"}]

    def test_accent_tranche_par_la_transcription(self):
        out = resolve_alternatives({"term": "Zabeo / Zabéo", "variants": []}, TRANSCRIPT)
        assert out[0]["term"] == "Zabéo" and out[0]["variants"] == ["Zabeo"]

    def test_sans_transcription_premier_choix(self):
        out = resolve_alternatives({"term": "NetForm / net-form / Netform", "variants": []})
        # « Netform » ne diffère de la forme que par la casse : pas une variante (comme LexiconManager).
        assert out[0]["term"] == "NetForm" and out[0]["variants"] == ["net-form"]
        assert "premier choix" in out[0]["comment"]

    def test_graphies_quasi_identiques_toutes_deux_entendues_restent_une_entree(self):
        """« Power BI / Power B » avec les deux dans le brut : la STT a produit les deux, l'une
        est la faute de l'autre → forme = première citée, l'autre devient la variante."""
        out = resolve_alternatives({"term": "Power BI / Power B", "variants": []}, "du Power BI puis Power B")
        assert len(out) == 1 and out[0]["term"] == "Power BI" and out[0]["variants"] == ["Power B"]

    def test_deux_formes_entendues_sont_deux_entites(self):
        """« PEI 25 / PEI 26 » : les deux sont dans la transcription → deux entrées, pas un
        remplacement de l'une par l'autre."""
        out = resolve_alternatives({"term": "PEI 25 / PEI 26", "variants": []}, TRANSCRIPT)
        assert [e["term"] for e in out] == ["PEI 25", "PEI 26"] and all(e["variants"] == [] for e in out)

    def test_toutes_absentes_premier_choix(self):
        out = resolve_alternatives({"term": "Lexing / Lexig", "variants": []}, TRANSCRIPT)
        assert out[0]["term"] == "Lexing" and out[0]["variants"] == ["Lexig"] and "premier choix" in out[0]["comment"]

    def test_entites_distinctes_scindees(self):
        out = resolve_alternatives({"term": "MSSI / DPO", "variants": ["M.S.S.I.", "dépéo"], "category": "sigle",
                                    "contexts": [{"text": "Le MSSI a validé"}, {"text": "le DPO aussi"}, {"text": "autre"}]}, TRANSCRIPT)
        assert [e["term"] for e in out] == ["MSSI", "DPO"]
        assert out[0]["variants"] == ["M.S.S.I."] and out[1]["variants"] == ["dépéo"]
        assert out[0]["contexts"] == [{"text": "Le MSSI a validé"}] and out[1]["contexts"] == [{"text": "le DPO aussi"}]
        assert all("scindée" in e["comment"] for e in out) and all(e["category"] == "sigle" for e in out)

    def test_mixte_graphies_et_entite(self):
        """Sans transcription, « COMET » ressemble assez à « Cometal » pour être une graphie ;
        avec une transcription où les deux sont entendus, ce sont deux entités."""
        out = resolve_alternatives({"term": "Cometal / COMETAL / COMET", "variants": []}, "")
        assert [e["term"] for e in out] == ["Cometal"] and out[0]["variants"] == ["COMET"]
        out = resolve_alternatives({"term": "Cometal / COMETAL / COMET", "variants": []}, "le Cometal puis le COMET")
        assert [e["term"] for e in out] == ["Cometal", "COMET"]
        assert out[0]["variants"] == [] and out[1]["variants"] == []

    def test_variante_sans_ressemblance_rejoint_le_premier_groupe_en_mention(self):
        out = resolve_alternatives({"term": "AVES / Vésinat", "variants": ["système d'archivage"]}, TRANSCRIPT)
        assert out[0]["term"] == "AVES" and out[0]["mentions"] == ["système d'archivage"]


def test_normalize_suspect_terms_separe_les_mentions_partout():
    terms = [
        {"term": "S2", "variants": ["semestre 2", "s 2"], "category": "période"},
        {"term": "Power BI / Power B", "variants": []},
        "pas un dict",
    ]
    out = normalize_suspect_terms(terms, TRANSCRIPT)
    assert out[0]["variants"] == ["s 2"] and out[0]["mentions"] == ["semestre 2"]
    assert out[1]["term"] == "Power BI" and out[1]["variants"] == ["Power B"]
    assert out[2] == "pas un dict" and len(out) == 3      # forme historique (chaîne) rendue telle quelle


def test_parse_summary_term_line_slash_colle_n_est_pas_decoupe():
    parsed = parse_summary_term_line("- **24/7** [expression] (normale) | commentaire: astreinte")
    assert parsed["term"] == "24/7" and parsed["variants"] == []
    parsed = parse_summary_term_line("- **Zabéo / Zabeo** [outil] (importante) | commentaire: outil")
    assert parsed["term"] == "Zabéo" and parsed["variants"] == ["Zabeo"]


def test_merge_llm_suggestions_normalise_avec_la_transcription():
    ctx: dict = {}
    merge_llm_suggestions(ctx, {"termes_suspects": [{"term": "Power BI / Power B", "variants": []}]},
                          transcript_text=TRANSCRIPT)
    assert ctx["termes_suspects"][0]["term"] == "Power BI"
    assert ctx["termes_suspects"][0]["variants"] == ["Power B"]


def test_session_lexicon_save_conserve_les_mentions(tmp_path):
    from transcria.context.lexicon import LexiconManager
    from transcria.jobs.models import Job
    job = Job(id="job-mentions", owner_id="u", title="t")
    (tmp_path / "job-mentions").mkdir()
    saved = LexiconManager.save(job, str(tmp_path), [
        {"term": "S2", "variants": ["s 2"], "mentions": ["semestre 2", "s 2", "S2"]},
        {"term": "AG", "variants": []},
    ])
    assert saved[0]["variants"] == ["s 2"] and saved[0]["mentions"] == ["semestre 2"]
    assert "mentions" not in saved[1]
    on_disk = json.loads((tmp_path / "job-mentions" / "context" / "session_lexicon.json").read_text(encoding="utf-8"))
    assert on_disk[0]["mentions"] == ["semestre 2"]


def test_les_mentions_attestent_la_presence_mais_ne_remplacent_pas():
    lexicon = [{"term": "S2", "variants": [], "mentions": ["semestre 2"], "priority": "normale"}]
    kept, meta = filter_lexicon_by_srt_presence(lexicon, "Le semestre 2 commence.")
    assert meta["kept_by_variant_presence"] == 1 and kept[0]["variants"] == []
    shown, meta2 = prefilter_lexicon_entries_for_display(lexicon, "Le semestre 2 commence.")
    assert meta2["kept_by_variant_presence"] == 1 and shown[0]["_display_reason"] == "variant_presence"
    merged = merge_lexicon_entries([], [{"term": "S2", "mentions": ["semestre 2"]}], [{"term": "S2", "mentions": ["sem. 2"]}])
    assert sorted(merged[0]["mentions"]) == ["sem. 2", "semestre 2"]
