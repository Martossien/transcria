"""Garde C2.2 — TOUTE clé de configuration doit être CLASSÉE (anti-divergence).

Une clé ajoutée aux défauts sans classification = échec CI : l'auteur doit décider
consciemment si elle est exposée dans le formulaire admin, interne (justifiée), ou
différée (à instruire). C'est la garde qui empêche l'écart formulaire/config de se
recreuser (constat d'entrée : 423 clés pour 27 champs exposés).
"""
from __future__ import annotations

from pathlib import Path

import yaml

from transcria.config.loader import _DEFAULT_CONFIG

_CLASSIFICATION = Path("transcria/data/config_classification.yaml")


def _leaves(d, prefix=""):
    for k, v in d.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            yield from _leaves(v, path + ".")
        else:
            yield path


def _load():
    return yaml.safe_load(_CLASSIFICATION.read_text(encoding="utf-8"))


class TestClassificationComplete:
    def test_toute_cle_des_defauts_est_classee(self):
        doc = _load()
        classified = set(doc.get("exposed", [])) | set(doc.get("internal", {})) | set(doc.get("deferred", []))
        missing = sorted(set(_leaves(_DEFAULT_CONFIG)) - classified)
        assert not missing, (
            "Clés de config NON CLASSÉES (ajoutez-les à transcria/data/config_classification.yaml "
            f"en décidant exposed/internal/deferred) : {missing}")

    def test_pas_de_cle_fantome(self):
        # une clé classée qui n'existe plus dans les défauts = classification périmée
        doc = _load()
        classified = set(doc.get("exposed", [])) | set(doc.get("internal", {})) | set(doc.get("deferred", []))
        real = set(_leaves(_DEFAULT_CONFIG))
        ghosts = sorted(classified - real)
        assert not ghosts, f"Clés classées mais absentes des défauts (à retirer) : {ghosts}"

    def test_exposed_couvre_le_formulaire(self):
        # tout champ du formulaire admin doit être classé exposed (cohérence)
        from transcria.web.config_form import CONFIG_FORM_SECTIONS, iter_fields
        doc = _load()
        exposed = set(doc.get("exposed", []))
        real = set(_leaves(_DEFAULT_CONFIG))
        form_paths = {f["path"] for f in iter_fields(CONFIG_FORM_SECTIONS)} & real
        missing = sorted(form_paths - exposed)
        assert not missing, f"Champs du formulaire non classés exposed : {missing}"

    def test_internal_porte_une_raison(self):
        doc = _load()
        for path, reason in (doc.get("internal") or {}).items():
            assert str(reason).strip(), f"{path} : classée internal SANS raison"
