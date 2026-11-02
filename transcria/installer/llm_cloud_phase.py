"""Phase « llm-cloud » de l'installateur — LLM d'arbitrage chez un fournisseur externe.

Opt-in (``install.sh --llm-cloud``, jamais par défaut ; docs/LLM_CLOUD.md § 5). Orchestration :

1. retrouve les identifiants déjà connus d'opencode SOUS LE HOME DU COMPTE DE SERVICE
   (``opencode auth export`` en v2, ``auth.json`` en v1) et ne garde que les fournisseurs de
   NOTRE table (``transcria/data/llm_providers.yaml``) — piège root vs compte admin : c'est le
   HOME du service qui compte, pas celui de l'opérateur qui lance l'installation ;
2. sinon demande le fournisseur et la clé (saisie masquée) — ou les prend en ligne de commande
   (``--provider`` + ``--key-env VAR`` : la clé est lue dans la variable VAR de l'environnement,
   jamais passée en argument) ;
3. écrit la variable du fournisseur dans ``.env`` (valeur entre guillemets simples, 0600,
   propriétaire = compte de service) — la clé n'est JAMAIS affichée ni journalisée, et n'entre
   jamais dans ``config.yaml`` ;
4. règle ``services.backend: cloud`` et les DEUX ``model_id`` (écriture chirurgicale, diff
   minimal, mode et propriétaire de config.yaml conservés, schéma revérifié) ;
5. pose ``share: disabled`` + la politique de permissions headless dans ``opencode.json`` (et le
   bloc ``provider`` d'un fournisseur hors table) ;
6. N'ÉCRIT PAS la validation admin (elle vit en base, faite par un admin connecté dans
   Administration → LLM cloud) mais le dit dans le récapitulatif.

Sous-processus, saisies et ``chown`` sont INJECTÉS : l'orchestration se teste sans binaire
opencode, sans terminal et sans privilèges (``tests/test_installer_llm_cloud.py``).
"""
from __future__ import annotations

import contextlib
import copy
import getpass
import json
import os
import pwd
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from transcria.cli_i18n import make_translator
from transcria.config.checks.llm_cloud import _is_local_host
from transcria.config.config_schema import validate_config
from transcria.config.env_file import get_env_value, update_env_file
from transcria.config.llm_providers import (
    CLOUD_BACKEND,
    LOCAL_PROVIDER_ID,
    CatalogProvider,
    LlmProvider,
    catalog_path_override,
    get_catalog_provider,
    load_llm_providers,
    opencode_models_cache_path,
)
from transcria.config.yaml_file import get_yaml_value, load_yaml_file, set_yaml_value
from transcria.installer.messages import INSTALL_MESSAGES
from transcria.installer.yaml_text_edit import YamlTextEditError, edit_yaml_text, write_text_preserving_owner
from transcria.llm_tools.opencode_cli import parse_opencode_version
from transcria.llm_tools.opencode_setup import (
    ensure_agent_permissions,
    ensure_cloud_provider,
    ensure_share_disabled,
    find_opencode_binary,
)
from transcria.workflow.agent_workspace import resolve_agent_work_root

# (code retour, stdout) — stderr n'est jamais lu : rien d'un sous-processus n'est réaffiché.
CommandRunner = Callable[[list[str], Mapping[str, str]], tuple[int, str]]
PromptFn = Callable[[str], str]
ChownFn = Callable[[Path, str], None]

MODEL_KEYS = ("workflow.summary_llm.model_id", "workflow.arbitration_llm.model_id")
ENABLED_KEYS = ("workflow.summary_llm.enabled", "workflow.arbitration_llm.enabled")
_PROVIDER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")
# Une clé d'API est un jeton : ni blanc, ni guillemet, ni caractère de contrôle. Le refus
# protège le quoting simple du .env (sourcé par start.sh, lu par systemd et python-dotenv).
_KEY_FORBIDDEN_RE = re.compile(r"[\s'\"\\\x00-\x1f\x7f]")
_EXPORT_TIMEOUT_S = 60
# Une saisie qui ne ressemble pas à un identifiant court n'est JAMAIS réaffichée : l'opérateur
# peut y avoir collé sa clé par erreur.
_ECHOABLE_RE = re.compile(r"^[a-z0-9-]{1,32}$")
_PROVIDER_TRIES = 3


class LlmCloudPhaseError(RuntimeError):
    """Échec actionnable de la phase (message SANS secret)."""


class _ConsoleLike(Protocol):
    def info(self, message: str) -> None: ...
    def ok(self, message: str) -> None: ...
    def warn(self, message: str) -> None: ...
    def error(self, message: str) -> None: ...


@dataclass(frozen=True)
class LlmCloudPlan:
    config_path: Path
    env_file: Path
    user_home: Path                       # HOME du compte de SERVICE (jamais celui de l'opérateur)
    service_user: str = ""
    provider: str = ""                    # --provider (sinon détecté / demandé)
    key_env: str = ""                     # --key-env : variable de l'ENVIRONNEMENT qui porte la clé
    model: str = ""                       # --model (sans ou avec le préfixe <fournisseur>/)
    api_base: str = ""                    # --api-base : fournisseur HORS table uniquement
    opencode_config: Path | None = None   # défaut : <user_home>/.config/opencode/opencode.json
    interactive: bool = True
    run_as_service_user: bool = False     # root qui installe pour un autre compte : runuser
    locale: str | None = None


@dataclass
class LlmCloudResult:
    provider: str = ""
    model: str = ""
    key_env: str = ""
    key_source: str = ""                  # opencode | prompt | environment | env-file
    recap: list[str] = field(default_factory=list)


def _default_runner(cmd: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    try:
        cp = subprocess.run(cmd, env=dict(env), capture_output=True, text=True,
                            timeout=_EXPORT_TIMEOUT_S, check=False)
    except (OSError, subprocess.SubprocessError):
        return 127, ""
    return cp.returncode, cp.stdout or ""


def _default_chown(path: Path, service_user: str) -> None:
    if not service_user or os.geteuid() != 0:
        return
    try:
        user = pwd.getpwnam(service_user)
    except KeyError:
        return
    with contextlib.suppress(OSError):
        os.chown(path, user.pw_uid, user.pw_gid)


def _default_prompt(message: str) -> str:
    try:
        return input(message)
    except EOFError:
        return ""


def _default_prompt_secret(message: str) -> str:
    try:
        return getpass.getpass(message)
    except EOFError:
        return ""


# ── Identifiants déjà connus d'opencode ─────────────────────────────────────────────

def _service_env(user_home: Path) -> dict[str, str]:
    """Environnement d'opencode « comme le service le verrait » : HOME du service, pas de XDG
    hérité de l'opérateur (sinon on lirait SES identifiants, pas ceux du service)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("XDG_")}
    env["HOME"] = str(user_home)
    return env


def _as_service_user(cmd: list[str], plan: LlmCloudPlan) -> list[str]:
    if not (plan.run_as_service_user and plan.service_user):
        return cmd
    runuser = shutil.which("runuser")
    if runuser:
        return [runuser, "-u", plan.service_user, "--", *cmd]
    return ["sudo", "-u", plan.service_user, "-H", "--", *cmd]


def parse_v2_export(stdout: str) -> dict[str, str]:
    """``opencode auth export`` (v2) → ``{integrationID: clé}`` (clés d'API seulement).

    Format lu au source v2.2.5 : liste ``{integrationID, label, active, value: {type: "key",
    key}}``. Tolère un objet ``{"credentials": [...]}`` ou un mapping ``{id: {...}}``. Un
    identifiant OAuth (abonnement) n'est PAS une clé d'API : ignoré.
    """
    try:
        data = json.loads(stdout or "")
    except ValueError:
        return {}
    if isinstance(data, dict) and isinstance(data.get("credentials"), list):
        data = data["credentials"]
    entries: list[tuple[str, Any]] = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                entries.append((str(item.get("integrationID") or item.get("id") or ""), item))
    elif isinstance(data, dict):
        entries = [(str(k), v) for k, v in data.items()]
    found: dict[str, str] = {}
    for integration, item in entries:
        if not integration or not isinstance(item, dict):
            continue
        if item.get("active") is False and integration in found:
            continue
        raw_value = item.get("value")
        value: dict = raw_value if isinstance(raw_value, dict) else item
        if str(value.get("type") or "") not in ("key", "api"):
            continue
        key = str(value.get("key") or "")
        if key:
            found[integration.strip().lower()] = key
    return found


def parse_v1_auth_json(text: str) -> dict[str, str]:
    """``auth.json`` (v1) → ``{fournisseur: clé}`` — entrées ``{type: "api", key}`` seulement."""
    try:
        data = json.loads(text or "")
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    found: dict[str, str] = {}
    for pid, item in data.items():
        if isinstance(item, dict) and item.get("type") == "api" and item.get("key"):
            found[str(pid).strip().lower()] = str(item["key"])
    return found


def discover_opencode_keys(
    plan: LlmCloudPlan, *, opencode_bin: str | None, runner: CommandRunner,
) -> dict[str, str]:
    """Clés d'API déjà enregistrées dans opencode POUR LE COMPTE DE SERVICE.

    Retour en mémoire uniquement — jamais écrit, jamais affiché. Toute panne (binaire absent,
    sortie illisible, délai) rend ``{}`` : la phase retombe sur la saisie.
    """
    if not opencode_bin:
        return {}
    env = _service_env(plan.user_home)
    rc, out = runner(_as_service_user([opencode_bin, "--version"], plan), env)
    version = parse_opencode_version(out if rc == 0 else "")
    if version.line >= 2:
        # --standalone : serveur privé qui meurt avec la commande — jamais le serveur partagé
        # d'un autre compte (cf. opencode_cli).
        rc, out = runner(_as_service_user([opencode_bin, "auth", "export", "--standalone"], plan), env)
        return parse_v2_export(out) if rc == 0 else {}
    auth_json = plan.user_home / ".local" / "share" / "opencode" / "auth.json"
    try:
        return parse_v1_auth_json(auth_json.read_text(encoding="utf-8"))
    except OSError:
        return {}


# ── Écritures ───────────────────────────────────────────────────────────────────────

def _set_config_values(path: Path, values: dict[str, Any], t: Callable[..., str]) -> str:
    """Pose les valeurs (diff minimal, mode/propriétaire conservés) ; rend le texte ORIGINAL."""
    try:
        original = path.read_text(encoding="utf-8")
        write_text_preserving_owner(path, edit_yaml_text(original, values))
    except (OSError, YamlTextEditError) as exc:
        raise LlmCloudPhaseError(t("lc_config_write_failed", path=path, error=exc)) from exc
    return original


def _new_errors(before: dict, after: dict) -> list[str]:
    """Erreurs de schéma INTRODUITES par nos valeurs (une erreur préexistante ne bloque pas)."""
    known = set(validate_config(before).errors)
    return [e for e in validate_config(after).errors if e not in known]


def _echoable(value: str) -> str:
    return value if _ECHOABLE_RE.match(value) else "…"


def _check_api_base(api_base: str, t: Callable[..., str]) -> str:
    url = api_base.strip().rstrip("/")
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        host = ""
    if not url.startswith("https://") or _is_local_host(host):
        raise LlmCloudPhaseError(t("lc_api_base_invalid"))
    return url


def _missing_dirs(path: Path, stop: Path) -> list[Path]:
    """Dossiers de ``path.parent`` à ``stop`` (exclu) qui n'existent pas encore — à chowner."""
    missing: list[Path] = []
    current = path.parent
    while current != stop and current != current.parent and not current.exists():
        missing.append(current)
        current = current.parent
    return missing


def _existing_cloud_model(cfg: dict, provider: str) -> str:
    current = str(get_yaml_value(cfg, "workflow.arbitration_llm.model_id") or "")
    prefix, _, model = current.partition("/")
    return model if prefix == provider and model else ""


def _validate_key(key: str, t: Callable[..., str]) -> str:
    key = key.strip()
    if not key:
        raise LlmCloudPhaseError(t("lc_key_empty"))
    if _KEY_FORBIDDEN_RE.search(key):
        raise LlmCloudPhaseError(t("lc_key_invalid"))
    return key


# ── Orchestration ───────────────────────────────────────────────────────────────────

def _choose_provider(
    plan: LlmCloudPlan, providers: Mapping[str, str], discovered: dict[str, str],
    existing: str, *, console: _ConsoleLike, prompt: PromptFn, t: Callable[..., str],
    check: Callable[[str], None], table_ids: frozenset[str] = frozenset(),
) -> str:
    if plan.provider:
        pid = plan.provider.strip().lower()
        check(pid)
        return pid
    if not plan.interactive:
        if existing:
            check(existing)
            return existing
        implicit = [pid for pid in discovered if pid in table_ids]
        if len(implicit) == 1 and len(discovered) == 1:
            # Un seul fournisseur connu d'opencode, et QUALIFIÉ : choix sans ambiguïté. Un
            # fournisseur du catalogue (non qualifié) ne se choisit jamais en silence.
            return implicit[0]
        raise LlmCloudPhaseError(t("lc_provider_required"))
    ordered = [pid for pid in providers if pid in discovered] + [pid for pid in providers if pid not in discovered]
    console.info(t("lc_provider_list"))
    for i, pid in enumerate(ordered, start=1):
        mark = t("lc_provider_found_mark") if pid in discovered else ""
        console.info(f"  {i}) {pid} — {providers[pid]}{mark}")
    default = ordered[0] if (discovered or not existing) else existing
    last_error: LlmCloudPhaseError | None = None
    for _ in range(_PROVIDER_TRIES):
        answer = prompt(t("lc_provider_prompt", default=default)).strip().lower() or default
        if answer.isdigit() and 1 <= int(answer) <= len(ordered):
            answer = ordered[int(answer) - 1]
        try:
            check(answer)
            return answer
        except LlmCloudPhaseError as exc:  # saisie invalide : on repose la question
            last_error = exc
            console.warn(str(exc))
    assert last_error is not None
    raise last_error


def _resolve_key(
    plan: LlmCloudPlan, provider_id: str, target_env: str, discovered: dict[str, str],
    *, console: _ConsoleLike, prompt: PromptFn, prompt_secret: PromptFn, t: Callable[..., str],
) -> tuple[str | None, str]:
    """(clé ou None = déjà dans .env, source). La clé ne sort JAMAIS de cette pile d'appels."""
    if plan.key_env:
        value = os.environ.get(plan.key_env, "")
        if not value:
            raise LlmCloudPhaseError(t("lc_key_env_missing", var=plan.key_env))
        return _validate_key(value, t), "environment"
    if provider_id in discovered:
        use = True
        if plan.interactive:
            use = not prompt(t("lc_key_found_prompt", provider=provider_id)).strip().lower().startswith("n")
        if use:
            return _validate_key(discovered[provider_id], t), "opencode"
    if os.environ.get(target_env):
        return _validate_key(os.environ[target_env], t), "environment"
    if get_env_value(plan.env_file, target_env):
        if not plan.interactive or prompt(t("lc_key_keep_prompt", var=target_env)).strip().lower() not in ("n", "non", "no"):
            return None, "env-file"
    if not plan.interactive:
        raise LlmCloudPhaseError(t("lc_key_required", var=target_env))
    console.info(t("lc_key_hidden_hint"))
    return _validate_key(prompt_secret(t("lc_key_prompt", var=target_env)), t), "prompt"


def _target_env(plan: LlmCloudPlan, provider_id: str, listed: LlmProvider | None,
                catalog: CatalogProvider | None, *, console: _ConsoleLike, t: Callable[..., str]) -> str:
    """Variable d'environnement qui portera la clé : imposée par la table, dérivée du catalogue
    models.dev (dérogation ``--key-env`` honorée), sinon exigée (fournisseur hors catalogue)."""
    if listed is not None:
        return listed.api_key_env
    if catalog is not None:
        if (plan.key_env or "").strip() and plan.key_env.strip() != catalog.api_key_env:
            # opencode lit la variable de models.dev : une autre variable laisserait les phases
            # agentiques SANS clé alors que nos appels directs seraient verts.
            raise LlmCloudPhaseError(t("lc_catalog_key_env_refused", provider=provider_id, var=catalog.api_key_env))
        console.info(t("lc_provider_from_catalog", provider=provider_id, url=catalog.base_url, var=catalog.api_key_env))
        return catalog.api_key_env
    target_env = (plan.key_env or "").strip()
    if not _ENV_NAME_RE.match(target_env):
        raise LlmCloudPhaseError(t("lc_unlisted_needs_key_env", provider=provider_id))
    return target_env


def _choose_model(plan: LlmCloudPlan, cfg: dict, provider_id: str, *, prompt: PromptFn,
                  t: Callable[..., str]) -> str:
    """Modèle nu (préfixe ``<fournisseur>/`` toléré puis retiré) : argument, sinon config
    existante, sinon saisie en interactif ; vide ou avec blanc = refus."""
    model = (plan.model or "").strip()
    if model.startswith(f"{provider_id}/"):
        model = model[len(provider_id) + 1:]
    if not model:
        model = _existing_cloud_model(cfg, provider_id)
        if plan.interactive:
            model = prompt(t("lc_model_prompt", provider=provider_id, default=model)).strip() or model
            if model.startswith(f"{provider_id}/"):
                model = model[len(provider_id) + 1:]
    if not model or any(c.isspace() for c in model):
        raise LlmCloudPhaseError(t("lc_model_required", provider=provider_id))
    return model


def apply_llm_cloud(
    plan: LlmCloudPlan,
    *,
    console: _ConsoleLike,
    runner: CommandRunner = _default_runner,
    prompt: PromptFn = _default_prompt,
    prompt_secret: PromptFn = _default_prompt_secret,
    chown: ChownFn = _default_chown,
    opencode_bin: str | None = None,
) -> LlmCloudResult:
    """Configure la LLM d'arbitrage cloud. Lève ``LlmCloudPhaseError`` (message sans secret)."""
    t = make_translator(INSTALL_MESSAGES, locale=plan.locale)
    if not plan.config_path.is_file():
        raise LlmCloudPhaseError(t("lc_config_missing", path=plan.config_path))
    providers = load_llm_providers()
    cfg = load_yaml_file(plan.config_path)

    existing_provider = ""
    if str(get_yaml_value(cfg, "services.backend") or "").strip().lower() == CLOUD_BACKEND:
        existing_provider = str(get_yaml_value(cfg, "workflow.arbitration_llm.model_id") or "").partition("/")[0]

    # Catalogue models.dev d'opencode SOUS LE HOME DU SERVICE (c'est lui qui résoudra le modèle).
    catalog_path = opencode_models_cache_path(plan.user_home)

    def _catalog(pid: str) -> CatalogProvider | None:
        return get_catalog_provider(pid, catalog_path)

    def _check_provider(pid: str) -> None:
        if pid == LOCAL_PROVIDER_ID or not _PROVIDER_ID_RE.match(pid):
            raise LlmCloudPhaseError(t("lc_provider_invalid", provider=_echoable(pid)))
        if pid not in providers and not plan.api_base and _catalog(pid) is None:
            raise LlmCloudPhaseError(t("lc_provider_unknown", provider=_echoable(pid), known=", ".join(providers)))

    # Tout ce qui peut être refusé l'est AVANT la moindre sonde ou écriture.
    if plan.provider:
        _check_provider(plan.provider.strip().lower())
    api_base = _check_api_base(plan.api_base, t) if plan.api_base else ""

    if opencode_bin is None:
        configured = str(get_yaml_value(cfg, "workflow.arbitration_llm.opencode_bin") or "")
        opencode_bin = find_opencode_binary(config_bin=configured or None, home=str(plan.user_home))
    if not opencode_bin:
        console.warn(t("lc_opencode_missing"))
    discovered: dict[str, str] = {}
    if not (plan.provider and plan.key_env):
        # Table qualifiée ET fournisseurs du catalogue (URL + variable connues d'opencode).
        discovered = {pid: key for pid, key in
                      discover_opencode_keys(plan, opencode_bin=opencode_bin, runner=runner).items()
                      if pid in providers or _catalog(pid) is not None}
        if discovered:
            console.ok(t("lc_found_in_opencode", home=plan.user_home, providers=", ".join(sorted(discovered))))
        else:
            console.info(t("lc_none_in_opencode", home=plan.user_home))

    menu = {pid: p.label for pid, p in providers.items()}
    for pid in sorted(discovered):
        from_catalog = _catalog(pid)
        if pid not in menu and from_catalog is not None:
            menu[pid] = from_catalog.label + t("lc_provider_catalog_mark")
    provider_id = _choose_provider(plan, menu, discovered, existing_provider,
                                   console=console, prompt=prompt, t=t, check=_check_provider,
                                   table_ids=frozenset(providers))
    listed = providers.get(provider_id)
    catalog = None if listed is not None else _catalog(provider_id)
    target_env = _target_env(plan, provider_id, listed, catalog, console=console, t=t)
    model = _choose_model(plan, cfg, provider_id, prompt=prompt, t=t)

    # (c, en mémoire) valeurs de config + contrôle du schéma AVANT toute écriture.
    model_ref = f"{provider_id}/{model}"
    values: dict[str, Any] = {"services.backend": CLOUD_BACKEND}
    for key_path in MODEL_KEYS:
        values[key_path] = model_ref
    for key_path in ENABLED_KEYS:
        values[key_path] = True
    if listed is None and catalog is None:
        values["workflow.arbitration_llm.api_base"] = api_base
        values["workflow.llm_cloud.api_key_env"] = target_env
    projected = copy.deepcopy(cfg)
    for key_path, value in values.items():
        set_yaml_value(projected, key_path, value)
    # Le schéma lit le catalogue SANS chemin : on lui impose celui du compte de service (sinon
    # c'est le cache de l'opérateur qui décide, et la config écrite peut être invalide au démarrage).
    with catalog_path_override(catalog_path):
        errors = _new_errors(cfg, projected)
    if errors:
        raise LlmCloudPhaseError(t("lc_schema_invalid", errors="; ".join(errors)))

    key, source = _resolve_key(plan, provider_id, target_env, discovered,
                               console=console, prompt=prompt, prompt_secret=prompt_secret, t=t)
    discovered.clear()  # plus aucune clé en mémoire au-delà de l'écriture

    # (b) .env : guillemets SIMPLES (robustes au `source` de start.sh), 0600, compte de service.
    if key is not None:
        try:
            update_env_file(plan.env_file, target_env, f"'{key}'", backup=False,
                            comment=t("lc_env_comment", provider=provider_id))
        except OSError as exc:
            raise LlmCloudPhaseError(t("lc_env_write_failed", path=plan.env_file, error=exc.strerror or exc)) from None
        finally:
            del key
        chown(plan.env_file, plan.service_user)
        console.ok(t("lc_env_written", var=target_env, path=plan.env_file))
    else:
        console.ok(t("lc_env_kept", var=target_env, path=plan.env_file))
    with contextlib.suppress(OSError):
        os.chmod(plan.env_file, 0o600)

    # (c) config.yaml : diff minimal, mode/propriétaire conservés, schéma revérifié sur le fichier.
    original_text = _set_config_values(plan.config_path, values, t)
    with catalog_path_override(catalog_path):
        errors = _new_errors(cfg, load_yaml_file(plan.config_path))
    if errors:
        with contextlib.suppress(OSError):
            write_text_preserving_owner(plan.config_path, original_text)
        raise LlmCloudPhaseError(t("lc_schema_invalid", errors="; ".join(errors)))
    console.ok(t("lc_config_written", model=model_ref))

    # (d) opencode.json : share disabled + permissions headless (+ bloc hors table). Les dossiers
    # créés (~/.config/opencode) reviennent au compte de service, pas seulement le fichier.
    oc_path = plan.opencode_config or (plan.user_home / ".config" / "opencode" / "opencode.json")
    created_dirs = _missing_dirs(oc_path, plan.user_home)
    try:
        if listed is None and catalog is None:
            ensure_cloud_provider(oc_path, provider_id, api_base, target_env, model)
        else:
            # Table ou catalogue : opencode résout le fournisseur seul (clé par variable d'env).
            ensure_share_disabled(oc_path)
        ensure_agent_permissions(oc_path, resolve_agent_work_root(load_yaml_file(plan.config_path)))
        for directory in (*created_dirs, oc_path):
            chown(directory, plan.service_user)
        console.ok(t("lc_opencode_written", path=oc_path))
    except (OSError, ValueError) as exc:
        console.warn(t("lc_opencode_failed", path=oc_path, error=exc))

    # (e)(f) récapitulatif : la validation reste à faire par un admin connecté.
    label = listed.label if listed else (catalog.label if catalog else provider_id)
    recap = [
        t("lc_recap_provider", label=label, model=model_ref),
        t("lc_recap_key", var=target_env, path=plan.env_file),
        t("lc_recap_validation"),
        t("lc_recap_restart"),
    ]
    if source == "opencode":
        recap.append(t("lc_recap_opencode_credential"))
    for line in recap:
        console.info(line)
    return LlmCloudResult(provider=provider_id, model=model_ref, key_env=target_env,
                          key_source=source, recap=recap)
