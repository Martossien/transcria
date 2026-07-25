"""L0 — stabilisation par local-agreement (partial→provisional)."""
from __future__ import annotations

from connector_service.live.agreement import LocalAgreement, Word


def _w(*texts):
    return [Word(t, i, i + 1) for i, t in enumerate(texts)]


def test_premiere_hypothese_ne_confirme_rien():
    la = LocalAgreement()
    assert la.insert(_w("bonjour", "le", "monde")) == []      # pas d'hypothèse antérieure
    assert la.committed == []


def test_prefixe_commun_confirme():
    la = LocalAgreement()
    la.insert(_w("bonjour", "le", "monde"))
    newly = la.insert(_w("bonjour", "le", "monde", "comment"))
    assert [w.text for w in newly] == ["bonjour", "le", "monde"]
    assert [w.text for w in la.committed] == ["bonjour", "le", "monde"]


def test_divergence_stoppe_la_confirmation():
    la = LocalAgreement()
    la.insert(_w("a", "b", "c"))
    newly = la.insert(_w("a", "X", "c"))                       # diverge au 2e mot
    assert [w.text for w in newly] == ["a"]


def test_confirmation_incrementale_sur_plusieurs_tours():
    la = LocalAgreement()
    la.insert(_w("bonjour", "le", "monde"))
    la.insert(_w("bonjour", "le", "monde", "comment"))        # confirme les 3 premiers
    newly = la.insert(_w("bonjour", "le", "monde", "comment", "ça"))
    assert [w.text for w in newly] == ["comment"]             # confirme "comment"


def test_partial_est_la_queue_instable():
    la = LocalAgreement()
    la.insert(_w("a", "b"))
    la.insert(_w("a", "b", "c"))                              # confirme a,b
    partial = la.partial(_w("a", "b", "c", "d"))
    assert [w.text for w in partial] == ["c", "d"]            # au-delà du confirmé


def test_finalize_promeut_la_queue():
    la = LocalAgreement()
    la.insert(_w("a", "b"))
    la.insert(_w("a", "b", "c"))                              # buffer = [c]
    rest = la.finalize()
    assert [w.text for w in rest] == ["c"]
    assert [w.text for w in la.committed] == ["a", "b", "c"]
