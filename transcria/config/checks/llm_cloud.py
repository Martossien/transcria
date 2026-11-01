"""Schéma de config — LLM d'arbitrage « cloud » (fournisseur externe, docs/LLM_CLOUD.md).

Vérificateur PAR DOMAINE (patron des `checks/`), appelé par le dispatcher ``validate_config``
avec la config ENTIÈRE : la cohérence cloud croise ``services.backend`` et les deux sections
LLM de ``workflow``.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse

from transcria.config.checks.base import ValidationResult
from transcria.config.llm_providers import (
    ARBITRAGE_BACKENDS,
    CLOUD_BACKEND,
    LOCAL_PROVIDER_ID,
    get_llm_provider,
)
from transcria.config.views import _split_model_ref

_ENV_VAR_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_LOCAL_HOSTNAMES = ("", "localhost")


def _is_local_host(host: str) -> bool:
    """Loopback (127.x, ::1) ou non spécifié (0.0.0.0, ::) — ``ipaddress`` plutôt qu'une liste."""
    if host in _LOCAL_HOSTNAMES:
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_unspecified


def _check_llm_cloud(cfg: dict, r: ValidationResult) -> None:
    """Valeur de ``services.backend`` et cohérence du mode cloud (§ 3.1.1, § 3.1.3, § 3.1.4).

    - ``services.backend`` hors des valeurs reconnues → ERREUR (jamais de repli silencieux :
      « clooud » deviendrait une LLM LOCALE — lancement de llama-server, sonde 127.0.0.1:8080,
      réservation VRAM) ;
    - en cloud : les ``model_id`` ACTIVÉS s'écrivent ``<fournisseur>/<modèle>``, partagent le
      MÊME fournisseur, jamais ``local/`` ; un fournisseur hors table exige la dérogation
      ``workflow.arbitration_llm.api_base`` (https, non loopback) ET ``workflow.llm_cloud.api_key_env`` ;
    - hors cloud : un ``model_id`` préfixé par un fournisseur de la table est un MÉLANGE refusé
      (il serait traité comme une LLM locale).

    Les scripts ``services.arbitrage_script``/``stop_script`` gardent leur validation historique
    (chemin non vide) : en cloud ils ne sont jamais exécutés, leurs défauts suffisent.
    """
    services = cfg.get("services") or {}
    raw_backend = services.get("backend")
    backend = str(raw_backend or "").strip().lower()
    # "" ≡ absent (auto-détection historique, cf. `configured_backend`) : jamais une erreur.
    if backend and backend not in ARBITRAGE_BACKENDS:
        r.add_error(
            f"services.backend: valeur inconnue '{raw_backend}' "
            f"(attendu : {', '.join(ARBITRAGE_BACKENDS)})"
        )
    wf = cfg.get("workflow") or {}
    sections = (("workflow.arbitration_llm", wf.get("arbitration_llm") or {}),
                ("workflow.summary_llm", wf.get("summary_llm") or {}))
    key_env = _check_llm_cloud_section(wf.get("llm_cloud"), r)

    if backend != CLOUD_BACKEND:
        for prefix, sec in sections:
            if not isinstance(sec, dict) or not sec.get("enabled"):
                continue
            provider = _split_model_ref(str(sec.get("model_id") or ""))[0]
            if provider and get_llm_provider(provider) is not None:
                r.add_error(
                    f"{prefix}.model_id: fournisseur cloud « {provider} » alors que services.backend "
                    f"n'est pas « cloud » — mélange local/cloud refusé (mettre services.backend: cloud)"
                )
        return

    providers: dict[str, str] = {}
    models: dict[str, str] = {}
    for prefix, sec in sections:
        if not isinstance(sec, dict) or not sec.get("enabled"):
            continue
        raw_model_id = str(sec.get("model_id") or "")
        provider, model = _split_model_ref(raw_model_id)
        if not provider or not model:
            r.add_error(
                f"{prefix}.model_id: en mode cloud, attendu « <fournisseur>/<modèle> » "
                f"(valeur: '{raw_model_id}')"
            )
            continue
        if raw_model_id.strip().split("/", 1)[0] != provider:
            # opencode compare les identifiants de fournisseur à la casse : « Mistral/x » passe
            # nos appels directs mais rend « Model unavailable » dans les phases agentiques.
            r.add_error(
                f"{prefix}.model_id: le fournisseur doit être en minuscules (« {provider}/… »), "
                f"valeur: '{raw_model_id}'"
            )
            continue
        if provider == LOCAL_PROVIDER_ID:
            r.add_error(
                f"{prefix}.model_id: « {LOCAL_PROVIDER_ID}/ » est la LLM locale — "
                "incompatible avec services.backend: cloud"
            )
            continue
        providers[prefix] = provider
        models[prefix] = model
    if len(set(providers.values())) > 1:
        detail = ", ".join(f"{k} → {v}" for k, v in providers.items())
        r.add_error(f"workflow: les model_id doivent viser le MÊME fournisseur cloud ({detail})")
    elif len(set(models.values())) > 1:
        # La validation admin, l'écran et la garde portent sur UN couple (fournisseur, modèle) :
        # un modèle de résumé différent partirait sans avoir été vu ni validé (relecture lot B).
        detail = ", ".join(f"{k} → {v}" for k, v in models.items())
        r.add_error(f"workflow: en cloud, résumé et correction doivent utiliser le MÊME modèle ({detail})")
    unlisted = next((p for p in providers.values() if get_llm_provider(p) is None), None)
    if unlisted is not None:
        _check_unlisted_provider(unlisted, wf.get("arbitration_llm") or {}, key_env, r)


def _check_llm_cloud_section(cloud_cfg: object, r: ValidationResult) -> str:
    """Forme de ``workflow.llm_cloud.*`` ; renvoie ``api_key_env`` (nettoyé, "" si absent)."""
    if cloud_cfg is None:
        return ""
    if not isinstance(cloud_cfg, dict):
        r.add_error("workflow.llm_cloud: doit être un objet YAML")
        return ""
    key_env = cloud_cfg.get("api_key_env", "")
    if key_env not in (None, "") and (
        not isinstance(key_env, str) or not _ENV_VAR_NAME.match(key_env.strip())
    ):
        r.add_error(
            "workflow.llm_cloud.api_key_env: nom de variable d'environnement invalide "
            "(lettres majuscules, chiffres, _)"
        )
        key_env = ""
    extra = cloud_cfg.get("extra_request_fields", {})
    if extra is not None and not isinstance(extra, dict):
        r.add_error("workflow.llm_cloud.extra_request_fields: doit être un objet YAML (champ → valeur)")
    return str(key_env or "").strip()


def _check_unlisted_provider(provider: str, arb: dict, key_env: str, r: ValidationResult) -> None:
    """Fournisseur NON listé : la dérogation doit être complète et sûre."""
    api_base = str(arb.get("api_base") or "").strip()
    try:
        host = urlparse(api_base).hostname or ""
    except ValueError:
        host = ""
    if not api_base.startswith("https://") or _is_local_host(host):
        r.add_error(
            f"workflow.arbitration_llm.api_base: fournisseur « {provider} » hors table — "
            f"une URL https:// non locale est requise (valeur: '{api_base}')"
        )
    if not key_env:
        r.add_error(
            f"workflow.llm_cloud.api_key_env: fournisseur « {provider} » hors table — "
            "le nom de la variable d'environnement portant la clé est requis"
        )
