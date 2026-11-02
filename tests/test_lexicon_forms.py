"""`apply_validated_forms` — variantes d'un lexique VALIDÉ → forme de référence (texte libre, rendu).

Pure, sans Flask : mot entier, casse ignorée, `replace_by` prioritaire sur `term`, variantes
longues d'abord, lexique vide/absent/malformé = texte intact.
"""
from transcria.context.lexicon import apply_validated_forms

LEX = [
    {"term": "Emmental", "variants": ["émental", "Emental"], "replace_by": ""},
    {"term": "SIGLE_A", "variants": ["abécé", "a b c"], "replace_by": "SIGLE_A"},
    {"term": "Organisation A", "variants": ["orga a"], "replace_by": "Organisation A (siège)"},
]


def test_mot_entier_casse_ignoree():
    assert apply_validated_forms("un peu d'émental, de l'EMENTAL et un émentalier", LEX) == \
        "un peu d'Emmental, de l'Emmental et un émentalier"


def test_replace_by_prime_et_variante_longue_d_abord():
    assert apply_validated_forms("réunion orga a avec a b c", LEX) == "réunion Organisation A (siège) avec SIGLE_A"


def test_variante_egale_a_la_forme_ignoree_et_entrees_malformees():
    lex = [{"term": "Comté", "variants": ["comté"]}, "pas un dict", {"variants": ["x"]}, {"term": "", "variants": ["y"]}]
    assert apply_validated_forms("du comté et x y", lex) == "du comté et x y"


def test_texte_vide_ou_lexique_absent():
    assert apply_validated_forms("", LEX) == ""
    assert apply_validated_forms("émental", None) == "émental"
    assert apply_validated_forms("émental", []) == "émental"
