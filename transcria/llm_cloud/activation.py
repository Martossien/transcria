"""Activation GRAPHIQUE de la LLM d'arbitrage — trois modes DÉCLARÉS par l'administrateur.

Le projet est fait pour être simple : « réglez config.yaml » n'est pas une réponse. Mais la nature
de la LLM ne se devine pas : une adresse publique peut être un réseau d'entreprise, une adresse
privée un relais vers un tiers. L'admin déclare donc ce qu'il branche, et c'est cette déclaration
qui gouverne tout le reste (avertissement, validation, bandeau, provenance) :

- ``local`` — sur cette machine (llama-server ou Ollama) : rien à déclarer ;
- ``organisation`` — un serveur OpenAI-compatible de l'organisation (LAN, VPN, plage achetée),
  clé optionnelle, http ou https : c'est une LLM **locale** du point de vue des données —
  aucune gouvernance cloud, un journal d'audit seulement ;
- ``cloud`` — un fournisseur externe (table qualifiée, catalogue opencode, ou hors table) :
  avertissement, validation administrateur, bandeau, interdiction par groupe, provenance.

Ce module PLANIFIE (pur : valide, projette la config, dit ce qui sera écrit) puis APPLIQUE
(``.env`` 0600 pour la clé, ``config.yaml`` par diff minimal, ``opencode.json``). La clé ne vit
qu'en mémoire le temps de l'écriture, n'est jamais journalisée ni renvoyée. La bascule prend
effet au redémarrage du service (``maintenance.restart_service``).
"""
from __future__ import annotations

import contextlib
import copy
import json
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from transcria.config.config_schema import validate_config
from transcria.config.env_file import get_env_value, update_env_file
from transcria.config.llm_providers import (
    CLOUD_BACKEND,
    CatalogProvider,
    LlmProvider,
    catalog_path_override,
    get_catalog_provider,
    load_llm_providers,
    opencode_models_cache_path,
)
from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_value
from transcria.installer.yaml_text_edit import YamlTextEditError, edit_yaml_text, write_text_preserving_owner
from transcria.llm_tools.opencode_setup import (
    ensure_agent_permissions,
    ensure_cloud_provider,
    ensure_local_provider,
    ensure_share_disabled,
)
from transcria.workflow.agent_workspace import resolve_agent_work_root

logger = logging.getLogger(__name__)

MODE_LOCAL = "local"
MODE_ORGANISATION = "organisation"
MODE_CLOUD = "cloud"
MODES = (MODE_LOCAL, MODE_ORGANISATION, MODE_CLOUD)

#: Variable d'environnement qui porte la clé d'un « serveur de mon organisation ».
ORG_KEY_ENV = "TRANSCRIA_ARBITRAGE_API_KEY"
#: La configuration LOCALE d'avant une bascule, remise telle quelle au retour (JSON dans config.yaml).
LOCAL_SNAPSHOT_KEY = "services.local_llm_snapshot"
SNAPSHOT_KEYS = (
    "services.backend", "services.arbitrage_llm_host", "services.arbitrage_llm_port",
    "services.arbitrage_api_model_id",
    "workflow.arbitration_llm.enabled", "workflow.arbitration_llm.model_id", "workflow.arbitration_llm.api_base",
    "workflow.arbitration_llm.api_key_env",
    "workflow.summary_llm.enabled", "workflow.summary_llm.model_id", "workflow.summary_llm.api_base",
    "workflow.llm_cloud.api_key_env",
)
MODEL_KEYS = ("workflow.summary_llm.model_id", "workflow.arbitration_llm.model_id")
ENABLED_KEYS = ("workflow.summary_llm.enabled", "workflow.arbitration_llm.enabled")

_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
#: Une clé est un jeton : ni blanc, ni guillemet, ni caractère de contrôle (quoting simple du .env).
_KEY_FORBIDDEN_RE = re.compile(r"[\s'\"\\\x00-\x1f\x7f]")
_KEY_MAX = 512
_LOCAL_HOSTS = ("", "127.0.0.1", "localhost", "::1")
#: Nom d'hôte ou adresse (IPv4, IPv6 nue) — rien d'autre ne va dans config.yaml ni dans un commentaire .env.
_HOST_RE = re.compile(r"^[A-Za-z0-9.\-:]{1,253}$")
#: Préfixe des variables dérivées pour un fournisseur hors table : un espace de noms à part,
#: jamais le nom d'un secret interne (TRANSCRIA_INFERENCE_API_KEY…) ni d'un fournisseur de la table.
_CUSTOM_KEY_PREFIX = "TRANSCRIA_CLOUD_"
#: Variables que l'écran ne doit JAMAIS écrire ni brancher comme clé de fournisseur.
_RESERVED_ENV = frozenset({
    "TRANSCRIA_SECRET", "TRANSCRIA_INFERENCE_API_KEY", "TRANSCRIA_STT_API_KEY", "TRANSCRIA_DATABASE_URL",
    "HF_TOKEN", "TRANSCRIA_ARBITRAGE_LLM_HOST",
})


class ActivationError(ValueError):
    """Refus AVANT toute écriture : ``code`` stable (message localisé côté web), ``message`` français."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ActivationRequest:
    mode: str
    # organisation
    api_base: str = ""
    model_alias: str = ""
    api_key: str = field(default="", repr=False)
    # cloud
    provider: str = ""
    model: str = ""
    summary_model: str = ""
    custom_api_base: str = ""


@dataclass
class ActivationPlan:
    mode: str
    values: dict[str, Any]
    key_env: str = ""
    key: str | None = field(default=None, repr=False)   # transitoire, jamais journalisé
    provider_label: str = ""
    model_ref: str = ""
    opencode: str = "local"                             # local | share | custom
    opencode_base_url: str = ""
    opencode_model: str = "arbitrage"
    warnings: list[str] = field(default_factory=list)
    key_provided: bool = field(init=False, default=False)

    def __post_init__(self) -> None:
        # Mémorisé à la planification : `apply_activation` efface la clé dès qu'elle est écrite.
        self.key_provided = self.key is not None

    def describe(self) -> dict:
        """Ce qui peut être journalisé : jamais la clé, seulement si elle a été fournie."""
        return {"mode": self.mode, "model_ref": self.model_ref, "key_env": self.key_env,
                "key_set": self.key_provided, "opencode": self.opencode}


@dataclass(frozen=True)
class ActivationResult:
    mode: str
    model_ref: str
    key_env: str
    key_written: bool
    opencode_written: bool
    warnings: tuple[str, ...]


# ── Lecture ──────────────────────────────────────────────────────────────────

def current_mode(cfg: dict) -> str:
    """Mode DÉCLARÉ par la configuration — jamais l'adresse : ``services.backend`` cloud → cloud ;
    ``http`` (un serveur que l'organisation gère, TranscrIA ne le lance pas) → organisation, même
    sur 127.0.0.1 ; sinon local (script, ollama, auto-détection)."""
    backend = str(get_yaml_value(cfg, "services.backend") or "").strip().lower()
    if backend == CLOUD_BACKEND:
        return MODE_CLOUD
    if backend == "http":
        return MODE_ORGANISATION
    return MODE_LOCAL


def organisation_settings(cfg: dict) -> dict:
    """Ce que l'écran pré-remplit pour le mode organisation (jamais la clé)."""
    return {
        "api_base": str(get_yaml_value(cfg, "workflow.arbitration_llm.api_base") or ""),
        "model_alias": str(get_yaml_value(cfg, "services.arbitrage_api_model_id") or "arbitrage"),
        "key_env": str(get_yaml_value(cfg, "workflow.arbitration_llm.api_key_env") or ""),
    }


def key_present(env_name: str, env_file: Path | None) -> bool:
    """La clé est-elle disponible : dans l'environnement de CE process ou dans ``.env`` ?"""
    if not env_name:
        return False
    if os.environ.get(env_name):
        return True
    if env_file is not None:
        try:
            return bool(get_env_value(env_file, env_name))
        except OSError:
            return False
    return False


def pending_restart(running_cfg: dict, file_cfg: dict) -> bool:
    """La configuration sur disque diffère-t-elle de celle que le process a chargée, sur les clés
    de la LLM d'arbitrage ? Si oui, la bascule attend un redémarrage."""
    return any(get_yaml_value(running_cfg, k) != get_yaml_value(file_cfg, k) for k in SNAPSHOT_KEYS)


# ── Planification (PURE) ─────────────────────────────────────────────────────

def _check_key(key: str) -> str:
    key = (key or "").strip()
    if not key:
        return ""
    if len(key) > _KEY_MAX or _KEY_FORBIDDEN_RE.search(key):
        raise ActivationError("key", "Clé d'API invalide (caractères interdits ou longueur excessive).")
    return key


def _check_org_url(raw: str) -> tuple[str, str, int]:
    url = (raw or "").strip().rstrip("/")
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme not in ("http", "https") or not host or not _HOST_RE.match(host):
        raise ActivationError("api_base", "Adresse du serveur invalide : attendu http(s)://hôte[:port]/v1.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.params:
        raise ActivationError("api_base", "Adresse du serveur invalide : pas d'identifiants ni de paramètres dans l'URL.")
    path = parsed.path or ""
    if path not in ("", "/v1"):
        # Les sondes reconstruisent <schéma>://<hôte>:<port>/v1/… : un préfixe de chemin les ferait
        # sonder une autre URL qu'opencode — refusé plutôt qu'incohérent.
        raise ActivationError("api_base", "Adresse du serveur invalide : le chemin doit être vide ou /v1.")
    try:
        port = parsed.port
    except ValueError:
        raise ActivationError("api_base", "Adresse du serveur invalide : port hors bornes.") from None
    if port is None:
        port = 443 if parsed.scheme == "https" else 80
    if not path:
        url = f"{url}/v1"
    return url, host, port


def _snapshot_values(cfg: dict) -> dict[str, Any]:
    snapshot = {k: get_yaml_value(cfg, k) for k in SNAPSHOT_KEYS}
    return {k: v for k, v in snapshot.items() if v is not None}


def _load_snapshot(cfg: dict) -> dict[str, Any] | None:
    raw = get_yaml_value(cfg, LOCAL_SNAPSHOT_KEY)
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None
    return None


def _plan_local(cfg: dict) -> ActivationPlan:
    values: dict[str, Any] = {}
    snapshot = _load_snapshot(cfg)
    if snapshot:
        values.update({k: v for k, v in snapshot.items() if k in SNAPSHOT_KEYS})
    else:
        ollama_url = str(get_yaml_value(cfg, "services.ollama_url") or "")
        if ollama_url:
            backend, alias = "ollama", str(get_yaml_value(cfg, "services.ollama_model_name") or "arbitrage")
            port = urlparse(ollama_url).port or 11434
        else:
            backend, alias = "script", "arbitrage"
            port = int(get_yaml_value(cfg, "services.arbitrage_llm_port") or 8080)
        values.update({
            "services.backend": backend,
            "services.arbitrage_llm_host": "127.0.0.1",
            "services.arbitrage_llm_port": port,
            "services.arbitrage_api_model_id": alias,
            "workflow.arbitration_llm.model_id": f"local/{alias}",
            "workflow.arbitration_llm.api_base": f"http://127.0.0.1:{port}/v1",
            "workflow.summary_llm.model_id": f"local/{alias}",
            "workflow.summary_llm.api_base": f"http://127.0.0.1:{port}/v1",
        })
    values["workflow.arbitration_llm.api_key_env"] = ""
    alias = str(values.get("services.arbitrage_api_model_id") or "arbitrage")
    base = str(values.get("workflow.arbitration_llm.api_base") or "http://127.0.0.1:8080/v1")
    return ActivationPlan(mode=MODE_LOCAL, values=values, model_ref=f"local/{alias}", provider_label="",
                          opencode="local", opencode_base_url=base, opencode_model=alias)


def _plan_organisation(req: ActivationRequest, cfg: dict, env_has_key: Callable[[str], bool]) -> ActivationPlan:
    url, host, port = _check_org_url(req.api_base)
    alias = (req.model_alias or "arbitrage").strip()
    if not _ALIAS_RE.match(alias):
        raise ActivationError("model_alias", "Alias du modèle invalide (lettres, chiffres, . _ : -).")
    key = _check_key(req.api_key)
    key_env = ORG_KEY_ENV if (key or env_has_key(ORG_KEY_ENV)) else ""
    values: dict[str, Any] = {
        "services.backend": "http",
        "services.arbitrage_llm_host": host,
        "services.arbitrage_llm_port": port,
        "services.arbitrage_api_model_id": alias,
        "workflow.arbitration_llm.model_id": f"local/{alias}",
        "workflow.arbitration_llm.api_base": url,
        "workflow.arbitration_llm.api_key_env": key_env,
        "workflow.summary_llm.model_id": f"local/{alias}",
        "workflow.summary_llm.api_base": url,
    }
    for k in ENABLED_KEYS:
        values[k] = True
    warnings = []
    if os.environ.get("TRANSCRIA_ARBITRAGE_LLM_HOST"):
        warnings.append("La variable d'environnement TRANSCRIA_ARBITRAGE_LLM_HOST est posée sur ce service : "
                        "elle primera sur l'hôte saisi pour les sondes. Retirez-la du .env ou de l'unité systemd.")
    if host in _LOCAL_HOSTS:
        warnings.append("Adresse locale : dans ce mode, TranscrIA ne lance ni n'arrête cette LLM (elle doit tourner "
                        "en permanence). Pour qu'il la gère selon la mémoire GPU, choisissez « Sur cette machine ».")
    if url.startswith("http://") and host not in _LOCAL_HOSTS and key:
        warnings.append("La clé circulera en clair (http) sur votre réseau : préférez https si le serveur le permet.")
    return ActivationPlan(mode=MODE_ORGANISATION, values=values, key_env=key_env, key=key or None,
                          provider_label=host, model_ref=f"local/{alias}", opencode="local",
                          opencode_base_url=url, opencode_model=alias, warnings=warnings)


def _plan_cloud(req: ActivationRequest, cfg: dict, *, providers: dict[str, LlmProvider],
                catalog_lookup: Callable[[str], CatalogProvider | None],
                env_has_key: Callable[[str], bool]) -> ActivationPlan:
    pid = (req.provider or "").strip().lower()
    if not _PROVIDER_ID_RE.match(pid):
        raise ActivationError("provider", "Identifiant de fournisseur invalide.")
    model = (req.model or "").strip()
    if model.startswith(f"{pid}/"):
        model = model[len(pid) + 1:]
    if not model or not _MODEL_RE.match(model):
        raise ActivationError("model", "Modèle invalide.")
    summary = (req.summary_model or "").strip() or model
    if summary.startswith(f"{pid}/"):
        summary = summary[len(pid) + 1:]
    if not _MODEL_RE.match(summary):
        raise ActivationError("model", "Modèle de résumé invalide.")
    listed = providers.get(pid)
    catalog = None if listed is not None else catalog_lookup(pid)
    custom_base = ""
    if listed is not None:
        key_env, label, opencode = listed.api_key_env, listed.label, "share"
    elif catalog is not None:
        key_env, label, opencode = catalog.api_key_env, catalog.label, "share"
    else:
        custom_base = (req.custom_api_base or "").strip().rstrip("/")
        parsed = urlparse(custom_base)
        if parsed.scheme != "https" or not parsed.hostname or parsed.hostname in _LOCAL_HOSTS:
            raise ActivationError("custom_api_base", "Fournisseur hors table : une URL https:// non locale est requise.")
        key_env = _CUSTOM_KEY_PREFIX + re.sub(r"[^A-Z0-9]+", "_", pid.upper()).strip("_") + "_API_KEY"
        label, opencode = pid, "custom"
    if key_env in _RESERVED_ENV or not key_env:
        raise ActivationError("provider", "Ce fournisseur désigne une variable réservée du service : refusé.")
    key = _check_key(req.api_key)
    if not key and not env_has_key(key_env):
        raise ActivationError("key", f"Clé d'API requise : aucune valeur pour {key_env} dans l'environnement du service.")
    values: dict[str, Any] = {"services.backend": CLOUD_BACKEND,
                              "workflow.arbitration_llm.model_id": f"{pid}/{model}",
                              "workflow.summary_llm.model_id": f"{pid}/{summary}",
                              "workflow.arbitration_llm.api_key_env": ""}
    for k in ENABLED_KEYS:
        values[k] = True
    if opencode == "custom":
        values["workflow.arbitration_llm.api_base"] = custom_base
        values["workflow.llm_cloud.api_key_env"] = key_env
    return ActivationPlan(mode=MODE_CLOUD, values=values, key_env=key_env, key=key or None,
                          provider_label=label, model_ref=f"{pid}/{model}", opencode=opencode,
                          opencode_base_url=custom_base, opencode_model=model)


def plan_activation(req: ActivationRequest, cfg: dict, *, providers: dict[str, LlmProvider] | None = None,
                    catalog_lookup: Callable[[str], CatalogProvider | None] | None = None,
                    env_has_key: Callable[[str], bool] | None = None) -> ActivationPlan:
    """Valide la demande et projette ce qui sera écrit — sans rien écrire.

    Quitter le mode local pose une photographie des clés locales (``services.local_llm_snapshot``)
    que le retour en local remet telle quelle.
    """
    if req.mode not in MODES:
        raise ActivationError("mode", "Mode inconnu.")
    has_key = env_has_key or (lambda name: bool(os.environ.get(name)))
    if req.mode == MODE_LOCAL:
        plan = _plan_local(cfg)
    elif req.mode == MODE_ORGANISATION:
        plan = _plan_organisation(req, cfg, has_key)
    else:
        plan = _plan_cloud(req, cfg, providers=providers if providers is not None else load_llm_providers(),
                           catalog_lookup=catalog_lookup or (lambda pid: get_catalog_provider(pid)),
                           env_has_key=has_key)
    if req.mode != MODE_LOCAL and current_mode(cfg) == MODE_LOCAL:
        # À chaque départ : un réglage local fait entre deux allers-retours (port, modèle Ollama)
        # doit être celui que le retour remet, pas une photographie d'origine.
        plan.values[LOCAL_SNAPSHOT_KEY] = json.dumps(_snapshot_values(cfg), ensure_ascii=False)
    return plan


# ── Application ──────────────────────────────────────────────────────────────

def _owner_of(path: Path) -> tuple[int, int] | None:
    """(uid, gid) du fichier, sinon de son dossier — pour que le process web (root ici) ne vole pas
    un fichier qui appartient au compte d'administration."""
    for candidate in (path, path.parent):
        try:
            st = candidate.stat()
            return st.st_uid, st.st_gid
        except OSError:
            continue
    return None


def _restore_owner(path: Path, owner: tuple[int, int] | None) -> None:
    if owner is not None and os.geteuid() == 0:
        os.chown(path, owner[0], owner[1])


def _new_errors(before: dict, after: dict) -> list[str]:
    known = set(validate_config(before).errors)
    return [e for e in validate_config(after).errors if e not in known]


def _render_config_text(original: str, values: dict[str, Any]) -> str:
    """Nouveau texte de ``config.yaml`` : diff minimal (``edit_yaml_text``) ; si le fichier déjoue
    l'éditeur chirurgical (section en style flow ``{a: 1}``, ancre…), round-trip ruamel qui
    conserve commentaires et guillemets. Pur : rien n'est écrit ici."""
    try:
        return edit_yaml_text(original, values)
    except YamlTextEditError as first:
        try:
            import io

            from ruamel.yaml import YAML
        except ImportError:  # pragma: no cover — ruamel est une dépendance installée
            raise ActivationError("config", f"Écriture refusée : {first}") from first
        yaml = YAML()
        yaml.preserve_quotes = True
        yaml.indent(mapping=2, sequence=2, offset=0)
        data = yaml.load(original)
        if not isinstance(data, dict):
            raise ActivationError("config", f"Écriture refusée : {first}") from first
        for key_path, value in values.items():
            set_yaml_value(data, key_path, value)
        buf = io.StringIO()
        yaml.dump(data, buf)
        return buf.getvalue()


def apply_activation(plan: ActivationPlan, *, config_path: Path, env_file: Path, opencode_config: Path,
                     service_home: Path | None = None, chown: Callable[[Path], None] | None = None) -> ActivationResult:
    """Écrit, dans cet ordre : la clé (``.env`` 0600), ``config.yaml`` (texte calculé et validé AVANT
    toute écriture, schéma revérifié après, restauré en cas d'erreur), ``opencode.json`` (best-effort,
    avertissement sinon)."""
    if not config_path.is_file():
        raise ActivationError("config", f"Configuration introuvable : {config_path}")
    before = load_yaml_file(config_path)
    projected = copy.deepcopy(before)
    for key_path, value in plan.values.items():
        set_yaml_value(projected, key_path, value)
    catalog_path = opencode_models_cache_path(service_home) if service_home else None
    with catalog_path_override(catalog_path):
        errors = _new_errors(before, projected)
    if errors:
        raise ActivationError("schema", "Configuration refusée : " + " ; ".join(errors))
    # Le texte final est calculé AVANT d'écrire quoi que ce soit : un fichier que l'éditeur ne sait
    # pas réécrire ne laisse ni clé orpheline dans .env ni config à moitié modifiée.
    try:
        original = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ActivationError("config", f"Lecture de {config_path} impossible : {exc.strerror or exc}") from None
    new_text = _render_config_text(original, plan.values)

    def keep_owner(path: Path, owner: tuple[int, int] | None) -> None:
        if chown is not None:
            chown(path)
        else:
            _restore_owner(path, owner)

    key_written = False
    if plan.key:
        owner = _owner_of(env_file)
        try:
            label = re.sub(r"[^A-Za-z0-9 .:_\-]+", "", plan.provider_label or plan.mode)[:64]
            comment = f"Clé de la LLM d'arbitrage ({label}), posée depuis l'administration"
            update_env_file(env_file, plan.key_env, f"'{plan.key}'", backup=False, comment=comment)
        except OSError as exc:
            raise ActivationError("env", f"Écriture de {env_file} impossible : {exc.strerror or exc}") from None
        finally:
            plan.key = None
        with contextlib.suppress(OSError):
            os.chmod(env_file, 0o600)
        with contextlib.suppress(Exception):
            keep_owner(env_file, owner)
        key_written = True

    try:
        write_text_preserving_owner(config_path, new_text)
    except OSError as exc:
        raise ActivationError("config", f"Écriture de {config_path} impossible : {exc.strerror or exc}") from None
    with catalog_path_override(catalog_path):
        errors = _new_errors(before, load_yaml_file(config_path))
    if errors:
        with contextlib.suppress(OSError):
            write_text_preserving_owner(config_path, original)
        raise ActivationError("schema", "Configuration refusée après écriture, fichier restauré : " + " ; ".join(errors))

    warnings = list(plan.warnings)
    opencode_written = False
    oc_owner = _owner_of(opencode_config)
    try:
        written_cfg = load_yaml_file(config_path)
        if plan.opencode == "local":
            ensure_local_provider(opencode_config, plan.opencode_base_url, plan.opencode_model,
                                  api_key_env=plan.key_env)
        elif plan.opencode == "custom":
            ensure_cloud_provider(opencode_config, plan.model_ref.partition("/")[0], plan.opencode_base_url,
                                  plan.key_env, plan.opencode_model)
        else:
            ensure_share_disabled(opencode_config)
        ensure_agent_permissions(opencode_config, resolve_agent_work_root(written_cfg))
        with contextlib.suppress(Exception):
            keep_owner(opencode_config, oc_owner)
        opencode_written = True
    except (OSError, ValueError) as exc:
        warnings.append(f"opencode.json non réaligné ({exc}) — relancer scripts/setup_opencode.py.")
    logger.info("LLM d'arbitrage : mode %s écrit (%s) — redémarrage requis", plan.mode, plan.model_ref)
    return ActivationResult(mode=plan.mode, model_ref=plan.model_ref, key_env=plan.key_env,
                            key_written=key_written, opencode_written=opencode_written, warnings=tuple(warnings))
