"""Doctor — LLM d'arbitrage CLOUD (``services.backend: cloud``, docs/LLM_CLOUD.md § 3.3.1).

Un fournisseur externe ne se lance ni ne s'arrête : ce qui peut casser, c'est la CHAÎNE qui
y mène — fournisseur résolu, clé dans l'environnement du service, ``opencode.json`` qui ne
l'écrase pas et ne partage rien, identifiant stocké qui primerait, modèle connu du catalogue
d'opencode ET servi par le fournisseur, validation administrateur à jour en base. Le check
les parcourt DANS CET ORDRE et s'arrête au premier ``fail`` (le suivant n'aurait pas de sens) ;
les ``warn`` s'accumulent.

Toutes les sondes sont injectables. L'appel réseau au fournisseur (``GET {base}/models``) n'a
lieu QUE dans le doctor — jamais depuis une page web. La valeur de la clé n'apparaît dans
aucun détail ni conseil (elle est en plus masquée défensivement en sortie).
"""
from __future__ import annotations

import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

from transcria.config.views import LlmCloudView
from transcria.diagnostics.checks.common import (
    FAIL,
    OK,
    WARN,
    CheckResult,
    _resolve_database_uri,
    _t,
)
from transcria.gpu.arbitrage_endpoint import cloud_view, is_cloud_backend
from transcria.gpu.llm_backend import CloudLLMBackend
from transcria.llm_cloud.policy import CLOUD_NOTICE_TEXT_VERSION, ack_is_valid
from transcria.llm_tools.opencode_cli import parse_opencode_version
from transcria.llm_tools.opencode_setup import SHARE_POLICY, find_opencode_binary, resolve_config_path

_CATALOG_TIMEOUT_S = 15
_PROVIDER_TIMEOUT_S = 10
_VERSION_TIMEOUT_S = 15
_HTTP_STATUS = re.compile(r"statut HTTP (\d{3})")
# Motifs de type clé/jeton masqués en sortie, même tronqués (« sk-ab…wxyz ») ou étrangers à la clé lue.
_SECRET_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-.…*]{6,}"),
    re.compile(r"(Bearer)\s+[^\s,;)\]}\"']+", re.IGNORECASE),
)


class _Stop(Exception):
    """Premier ``fail`` rencontré : interrompt la chaîne de vérifications."""

    def __init__(self, detail: str, hint: str | None):
        super().__init__(detail)
        self.detail = detail
        self.hint = hint


def _section(cfg: dict, *keys: str) -> dict:
    """Sous-section de config (dict vide si absente) — sans chaîne ``get().get()``."""
    node: Any = cfg
    for key in keys:
        node = node.get(key) if isinstance(node, dict) else None
    return node if isinstance(node, dict) else {}


def _llm_sections(cfg: dict) -> tuple[dict, dict]:
    return _section(cfg, "workflow", "arbitration_llm"), _section(cfg, "workflow", "summary_llm")


# ── Sondes par défaut (monde extérieur) ───────────────────────────────────────


def _read_json_file(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _opencode_data_dir() -> str:
    """Dossier de données d'opencode tel que le verrait un opencode lancé par ce process."""
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "opencode")


def _opencode_auth_path() -> str:
    """``auth.json`` d'opencode v1."""
    return os.path.join(_opencode_data_dir(), "auth.json")


def _opencode_db_path() -> str:
    """``opencode.db`` d'opencode v2 (identifiants dans la table ``credential``)."""
    return os.path.join(_opencode_data_dir(), "opencode.db")


def _read_v2_credential_providers(path: str) -> set[str] | None:
    """Fournisseurs ayant un identifiant stocké dans ``opencode.db`` (v2) — lecture SEULE.

    Seule la colonne ``integration_id`` est lue : jamais la valeur d'un identifiant. Base
    absente ⇒ ``None`` (rien à signaler) ; base présente mais illisible ⇒ lève (signalé).
    """
    if not os.path.isfile(path):
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2)
    try:
        rows = conn.execute(
            "SELECT DISTINCT integration_id FROM credential WHERE integration_id IS NOT NULL"
        ).fetchall()
    finally:
        conn.close()
    return {str(r[0]) for r in rows}


def _kill_group(proc: subprocess.Popen) -> None:
    """Termine le GROUPE de process (wrapper npm, serveur privé v2) : SIGTERM puis SIGKILL."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.send_signal(sig)
            except ProcessLookupError:
                return
        try:
            proc.communicate(timeout=5)
            return
        except subprocess.TimeoutExpired:
            continue


def _run_isolated(cmd: list[str], *, env: dict[str, str], timeout: float) -> tuple[int, str]:
    """Lance ``cmd`` dans son propre groupe de process ; au délai dépassé, tue le GROUPE et lève."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
        text=True, env=env, start_new_session=True,
    )
    try:
        out, _err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(proc)
        raise TimeoutError(f"{timeout} s") from None
    return proc.returncode, out or ""


def _models_command(binary: str, env: dict[str, str]) -> list[str]:
    """``opencode models`` selon la ligne : v2 ⇒ ``--standalone`` (jamais le service partagé).

    Sans ``--standalone``, une v2 démarre/réutilise le serveur d'arrière-plan PARTAGÉ, qui
    survit au doctor et ignore le ``XDG_DATA_HOME`` isolé — effet de bord interdit ici.
    """
    _code, out = _run_isolated([binary, "--version"], env=env, timeout=_VERSION_TIMEOUT_S)
    version = parse_opencode_version(out)
    if version.line >= 2:
        return [binary, "models", "--standalone"]
    return [binary, "models"]


def _probe_opencode_catalog(cfg: dict) -> list[str]:
    """``opencode models`` → liste ``<fournisseur>/<modèle>``. Lève si le binaire ne répond pas.

    ``XDG_DATA_HOME`` est un dossier VIDE (comme chaque run du pipeline) : le catalogue lu est
    celui que verra le runner — snapshot embarqué + config, sans identifiant stocké. Le process
    vit dans son propre groupe, tué en bloc au délai dépassé (patron ``OpenCodeRunner``).
    """
    binary = find_opencode_binary(config_bin=_section(cfg, "workflow", "arbitration_llm").get("opencode_bin"))
    if not binary:
        raise FileNotFoundError("binaire opencode introuvable")
    with tempfile.TemporaryDirectory(prefix="transcria_doctor_catalog_") as data_home:
        env = {**os.environ, "XDG_DATA_HOME": data_home}
        code, out = _run_isolated(_models_command(binary, env), env=env, timeout=_CATALOG_TIMEOUT_S)
    if code != 0:
        raise RuntimeError(f"code de sortie {code}")
    return [line.strip() for line in out.splitlines() if "/" in line.strip()]


def _probe_provider_models(cfg: dict) -> tuple[list[str] | None, str | None]:
    """``GET {base}/models`` avec la clé (Bearer) — l'UNIQUE appel réseau au fournisseur."""
    return CloudLLMBackend(cfg).list_models(timeout=_PROVIDER_TIMEOUT_S)


def _read_cloud_ack(cfg: dict, provider: str, model: str) -> dict | None:
    """Validation la plus récente non révoquée pour (fournisseur, modèle), hors app Flask.

    Requête SQL directe (patron ``checks/database``) : le doctor n'a pas de contexte Flask.
    Lève si la base ou la table est injoignable.
    """
    from sqlalchemy import create_engine, text  # tiers, comme les sondes de base existantes

    engine = create_engine(_resolve_database_uri(cfg))
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT provider, model, text_version, revoked_at FROM llm_cloud_acknowledgements "
                    "WHERE provider = :provider AND model = :model AND revoked_at IS NULL "
                    "ORDER BY acknowledged_at DESC LIMIT 1"
                ),
                {"provider": provider.strip().lower(), "model": model.strip()},
            ).mappings().first()
    finally:
        engine.dispose()
    return dict(row) if row is not None else None


# ── Étapes ────────────────────────────────────────────────────────────────────


def _step_provider(cfg: dict, view: LlmCloudView) -> None:
    arb, summ = _llm_sections(cfg)
    arb_id = str(arb.get("model_id") or "").strip()
    summ_id = str(summ.get("model_id") or "").strip()
    shown = arb_id if arb.get("enabled") else (summ_id or arb_id)
    if not view.provider or not view.model:
        raise _Stop(_t("lc_no_provider", model_id=shown or "(vide)"), _t("lc_no_provider_hint"))
    if view.is_local_provider:
        raise _Stop(_t("lc_local_provider", model_id=shown), _t("lc_local_provider_hint"))
    if arb.get("enabled") and summ.get("enabled") and arb_id != summ_id:
        raise _Stop(_t("lc_ids_differ", arb=arb_id, summ=summ_id), _t("lc_ids_differ_hint"))
    if not view.base_url:
        raise _Stop(_t("lc_no_base_url", provider=view.provider), _t("lc_no_base_url_hint"))
    if not view.api_key_env:
        raise _Stop(_t("lc_no_key_env", provider=view.provider), _t("lc_no_key_env_hint"))


def _step_key(key: str | None, view: LlmCloudView) -> None:
    if not key:
        raise _Stop(_t("lc_key_missing", env=view.api_key_env), _t("lc_key_missing_hint", env=view.api_key_env))


def _step_opencode_config(view: LlmCloudView, path: str, data: dict | None) -> None:
    if data is None:
        raise _Stop(_t("lc_oc_config_missing", path=path), _t("lc_oc_config_hint"))
    share = data.get("share")
    if share != SHARE_POLICY:
        raise _Stop(_t("lc_share_not_disabled", path=path, share=share if share else "manual"),
                    _t("lc_oc_config_hint"))
    providers = data.get("provider")
    block = providers.get(view.provider) if isinstance(providers, dict) else None
    options = block.get("options") if isinstance(block, dict) else None
    api_key = options.get("apiKey") if isinstance(options, dict) else None
    if api_key is None:
        return
    if view.known_provider:
        raise _Stop(_t("lc_api_key_residual", path=path, provider=view.provider, env=view.api_key_env),
                    _t("lc_api_key_residual_hint"))
    if not str(api_key).startswith("{env:"):
        raise _Stop(_t("lc_api_key_literal", path=path, provider=view.provider), _t("lc_oc_config_hint"))


def _step_stored_credential(view: LlmCloudView, path: str, data: dict | None, warns: list) -> None:
    if data and view.provider in data:
        warns.append((_t("lc_auth_stored", provider=view.provider, path=path, env=view.api_key_env),
                      _t("lc_auth_stored_hint")))


def _step_stored_credential_v2(view: LlmCloudView, path: str,
                               reader: Callable[[str], set[str] | None], warns: list) -> None:
    try:
        providers = reader(path)
    except Exception as exc:  # noqa: BLE001 — base verrouillée/schéma inconnu : on le dit, sans bloquer
        warns.append((_t("lc_auth_db_unreadable", path=path, exc=type(exc).__name__),
                      _t("lc_auth_db_unreadable_hint")))
        return
    if providers and view.provider in providers:
        warns.append((_t("lc_auth_stored", provider=view.provider, path=path, env=view.api_key_env),
                      _t("lc_auth_stored_v2_hint")))


def _step_catalog(cfg: dict, model_id: str, view: LlmCloudView,
                  probe: Callable[[dict], list[str]], warns: list) -> None:
    try:
        catalog = probe(cfg)
    except Exception as exc:  # noqa: BLE001 — binaire muet/absent : on le dit, sans bloquer
        warns.append((_t("lc_catalog_unavailable", reason=type(exc).__name__ + f": {exc}"),
                      _t("lc_catalog_unavailable_hint")))
        return
    if model_id not in catalog:
        raise _Stop(_t("lc_catalog_missing", model_id=model_id, count=len(catalog)),
                    _t("lc_catalog_missing_hint", provider=view.provider))


def _step_provider_models(cfg: dict, view: LlmCloudView,
                          probe: Callable[[dict], tuple[list[str] | None, str | None]], warns: list) -> None:
    try:
        ids, err = probe(cfg)
    except Exception as exc:  # noqa: BLE001 — sonde réseau : jamais de crash
        ids, err = None, f"erreur inattendue: {type(exc).__name__}"
    if err or ids is None:
        reason = err or "réponse vide"
        match = _HTTP_STATUS.search(reason)
        status = int(match.group(1)) if match else 0
        if status in (401, 403):
            # Statut SEUL : le corps d'un refus peut citer la clé, même partiellement masquée.
            raise _Stop(_t("lc_provider_auth", provider=view.provider, reason=f"statut HTTP {status}"),
                        _t("lc_provider_auth_hint", env=view.api_key_env))
        if status:
            warns.append((_t("lc_provider_http", provider=view.provider, reason=reason),
                          _t("lc_provider_unreachable_hint", url=view.base_url)))
        else:
            warns.append((_t("lc_provider_unreachable", provider=view.provider, reason=reason),
                          _t("lc_provider_unreachable_hint", url=view.base_url)))
        return
    if ids and view.model not in ids:
        raise _Stop(_t("lc_provider_model_missing", model=view.model, provider=view.provider, count=len(ids)),
                    _t("lc_provider_model_missing_hint", provider=view.provider))


def _step_ack(cfg: dict, view: LlmCloudView, reader: Callable[[dict, str, str], dict | None]) -> None:
    try:
        ack = reader(cfg, view.provider, view.model)
    except Exception as exc:  # noqa: BLE001 — base/table injoignable : fail explicite
        raise _Stop(_t("lc_ack_unreadable", exc=type(exc).__name__), _t("lc_ack_unreadable_hint")) from None
    if not ack:
        raise _Stop(_t("lc_ack_missing", provider=view.provider, model=view.model), _t("lc_ack_hint"))
    valid = ack_is_valid(
        ack_provider=ack.get("provider"), ack_model=ack.get("model"),
        ack_text_version=ack.get("text_version"), ack_revoked=ack.get("revoked_at") is not None,
        provider=view.provider, model=view.model,
    )
    if not valid:
        raise _Stop(_t("lc_ack_stale", version=ack.get("text_version") or "?", current=CLOUD_NOTICE_TEXT_VERSION),
                    _t("lc_ack_hint"))


def _scrub_text(text: str, secret: str | None) -> str:
    if secret:
        text = text.replace(secret, "***")
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: f"{m.group(1)} ***" if m.groups() else "***", text)
    return text


def _scrub(result: CheckResult, secret: str | None) -> CheckResult:
    """Défense en profondeur : ni la clé, ni un motif de type clé/jeton ne sort du doctor."""
    result.detail = _scrub_text(result.detail, secret)
    if result.hint:
        result.hint = _scrub_text(result.hint, secret)
    return result


def _llm_enabled(cfg: dict) -> bool:
    arb, summ = _llm_sections(cfg)
    return bool(arb.get("enabled") or summ.get("enabled"))


def check_llm_cloud(
    cfg: dict,
    *,
    network: bool = True,
    key_reader: Callable[[dict], str | None] | None = None,
    opencode_config_path: str | None = None,
    opencode_config_reader: Callable[[str], dict | None] | None = None,
    auth_path: str | None = None,
    auth_reader: Callable[[str], dict | None] | None = None,
    db_path: str | None = None,
    db_reader: Callable[[str], set[str] | None] | None = None,
    catalog_probe: Callable[[dict], list[str]] | None = None,
    provider_probe: Callable[[dict], tuple[list[str] | None, str | None]] | None = None,
    ack_reader: Callable[[dict, str, str], dict | None] | None = None,
) -> CheckResult:
    """Chaîne complète de la LLM cloud (docs/LLM_CLOUD.md § 3.3.1). Hors cloud : OK « sans objet ».

    ``network=False`` (tier web) : fournisseur résolu + validation en base seulement — ni clé,
    ni opencode, ni appel au fournisseur (le web n'exécute aucune phase LLM).
    """
    name = _t("chk_llm_cloud") if network else _t("chk_llm_cloud_web")
    if not is_cloud_backend(cfg):
        return CheckResult(name, OK, _t("lc_not_cloud"))
    if not _llm_enabled(cfg):
        return CheckResult(name, OK, _t("lc_llm_disabled"))

    view = cloud_view(cfg)
    model_id = f"{view.provider}/{view.model}"
    key: str | None = None
    warns: list[tuple[str, str]] = []
    try:
        _step_provider(cfg, view)
        if network:
            key = (key_reader or (lambda c: CloudLLMBackend(c).api_key()))(cfg)
            _step_key(key, view)
            oc_path = opencode_config_path or resolve_config_path()
            _step_opencode_config(view, oc_path, (opencode_config_reader or _read_json_file)(oc_path))
            a_path = auth_path or _opencode_auth_path()
            _step_stored_credential(view, a_path, (auth_reader or _read_json_file)(a_path), warns)
            _step_stored_credential_v2(view, db_path or _opencode_db_path(),
                                       db_reader or _read_v2_credential_providers, warns)
            _step_catalog(cfg, model_id, view, catalog_probe or _probe_opencode_catalog, warns)
            _step_provider_models(cfg, view, provider_probe or _probe_provider_models, warns)
        _step_ack(cfg, view, ack_reader or _read_cloud_ack)
    except _Stop as stop:
        return _scrub(CheckResult(name, FAIL, stop.detail, hint=stop.hint), key)

    if not network:
        return _scrub(CheckResult(name, OK, _t("lc_ok_web", provider=view.provider, model=view.model)), key)
    ok_detail = _t("lc_ok", provider=view.provider, model=view.model, url=view.base_url, env=view.api_key_env)
    if warns:
        return _scrub(CheckResult(name, WARN, ok_detail + " ; " + " ; ".join(d for d, _ in warns),
                                  hint=" | ".join(h for _, h in warns)), key)
    return _scrub(CheckResult(name, OK, ok_detail), key)


def check_llm_cloud_web(cfg: dict, **kwargs: Any) -> CheckResult:
    """Variante ALLÉGÉE du tier web : sans réseau, sans clé — validation en base + fournisseur."""
    return check_llm_cloud(cfg, network=False, **kwargs)
