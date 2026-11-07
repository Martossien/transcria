"""Panne opencode RECONNUE → cause nommée → remède automatique quand il est à portée.

Vécu 2026-11-07 (première qualification cloud réelle) : opencode v1 (``opencode-ai`` 1.18.x)
rejette les chunks ``tool_calls`` SANS ``id`` que Mistral streame pour les modèles GLM —
``AI_TypeValidationError`` à CHAQUE appel d'outil, trois tentatives pour rien, et un bandeau
« modèle non résolu ou serveur en erreur » qui pointait ailleurs. La ligne v2 passe, les
modèles natifs du fournisseur passent avec la v1.

Ce module, à trois étages, tous testés sans réseau :

1. **reconnaître** la signature dans la sortie d'opencode (``classify_failure``, pur) ;
2. **chercher** un binaire de la ligne v2 DÉJÀ présent sur la machine, et **de confiance**
   (``find_v2_binary`` : propriétaire root, le compte du service, ou le propriétaire de
   ``config.yaml`` — celui qui décide déjà de ``opencode_bin`` ; jamais un fichier
   modifiable par les autres, jamais sous un répertoire temporaire) ;
3. **basculer** (``switch_binary``) : ``config.yaml`` (diff minimal, propriétaire conservé),
   la configuration en mémoire (effet immédiat pour les prochaines phases), un journal
   d'audit, et une trace sur disque qui rend la bascule **annulable** depuis l'écran
   « LLM d'arbitrage » (``pending_switch`` / ``revert_switch``).

Règle : rien n'est silencieux. Une bascule est journalisée en WARNING, auditée, affichée.
Sans v2 de confiance sur la machine, la panne reste nommée (``cause_text``) et l'écran propose
l'installation d'une copie privée épinglée (``installer.opencode_lib``).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import stat
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from transcria.config.loader import get_config, get_config_path
from transcria.installer.yaml_text_edit import YamlTextEditError, set_yaml_file_scalars
from transcria.llm_tools.opencode_cli import OpencodeVersion

logger = logging.getLogger(__name__)

#: La LLM est jointe, répond, appelle un outil — et le SDK embarqué dans CE binaire opencode
#: refuse la forme du flux du fournisseur. Ni la clé, ni le modèle, ni le réseau.
SDK_TOOL_CALL_INCOMPAT = "sdk_tool_call_incompat"

#: Classes de panne pour lesquelles RETENTER le même binaire est inutile (déterministes).
HARD_FAILURE_KINDS = frozenset({SDK_TOOL_CALL_INCOMPAT})

_KEY_BIN = "workflow.arbitration_llm.opencode_bin"
_KEY_PREVIOUS = "workflow.arbitration_llm.opencode_bin_previous"
_KEY_AT = "workflow.arbitration_llm.opencode_bin_switched_at"
_KEY_REASON = "workflow.arbitration_llm.opencode_bin_switch_reason"

_ID_FIELD = re.compile(r'\\?"id\\?"')


# ── 1. reconnaître ───────────────────────────────────────────────────────────

def classify_failure(stderr: str, events: list[dict] | None = None) -> str | None:
    """Classe de panne RECONNUE dans la sortie d'opencode, ou ``None`` (panne générique).

    La signature est celle du SDK (``AI_TypeValidationError``) : validation d'un chunk
    ``chat.completion.chunk`` dont ``choices[0].delta.tool_calls[0].id`` manque. Le texte
    peut arriver brut (stderr) ou JSON-échappé (événement ``error`` de ``--format json``) :
    les guillemets sont tolérés sous les deux formes.
    """
    chunks = [stderr or ""]
    for ev in events or ():
        if isinstance(ev, dict) and ev.get("type") == "error":
            try:
                chunks.append(json.dumps(ev, ensure_ascii=False))
            except (TypeError, ValueError):
                continue
    text = "\n".join(chunks)
    if ("Type validation failed" in text and "tool_calls" in text
            and "received undefined" in text and _ID_FIELD.search(text)):
        return SDK_TOOL_CALL_INCOMPAT
    return None


def _short(version: OpencodeVersion) -> str:
    """« 2.2.5 » — jamais le libellé brut (« opencode v2.2.5 » en v2)."""
    return f"{version.major}.{version.minor}.{version.patch}" if version.known else (version.raw or "?")


def cause_text(kind: str, *, version: OpencodeVersion | None = None) -> str:
    """Phrase FR qui NOMME la cause (journal, résultat de phase, écran) — jamais la clé."""
    if kind == SDK_TOOL_CALL_INCOMPAT:
        line = f" (v{_short(version)})" if version and version.known else ""
        return (
            f"le binaire opencode utilisé{line} ne sait pas lire les appels d'outil de ce modèle "
            "chez ce fournisseur (flux sans identifiant d'appel). Ce n'est ni la clé, ni le réseau, "
            "ni le texte : la ligne opencode v2 lit ce flux — installez-la depuis l'écran « LLM "
            "d'arbitrage » (ou choisissez un modèle natif du fournisseur)."
        )
    return kind


# ── 2. chercher un binaire v2 de confiance ───────────────────────────────────

def _default_candidates(home: str) -> list[str]:
    # Miroir de opencode_setup._default_candidates + la copie privée de l'installateur.
    return [
        f"{home}/.transcria/opencode/.opencode/bin/opencode",
        f"{home}/.opencode/bin/opencode",
        f"{home}/.npm-global/bin/opencode",
        f"{home}/node_modules/.bin/opencode",
        "/usr/local/bin/opencode",
        "/opt/homebrew/bin/opencode",
        "/usr/bin/opencode",
    ]


def candidate_binaries(
    *,
    home: str | None = None,
    path_env: str | None = None,
    globber: Callable[[str], list[str]] = glob.glob,
) -> list[str]:
    """Tous les emplacements où un opencode peut vivre, dans l'ordre, dédoublonnés (chemin réel).

    PATH du process, emplacements connus du HOME courant, copies privées et installations
    des autres comptes (``/home/*``) — le service tourne souvent sous un autre compte que
    l'administrateur qui a installé opencode (vécu : service root, opencode v2 chez l'admin).
    """
    home = home or os.path.expanduser("~")
    path_env = os.environ.get("PATH", "") if path_env is None else path_env
    raw: list[str] = [os.path.join(d, "opencode") for d in path_env.split(os.pathsep) if d]
    raw += _default_candidates(home)
    for pattern in ("/home/*/.transcria/opencode/.opencode/bin/opencode",
                    "/home/*/.opencode/bin/opencode", "/home/*/.npm-global/bin/opencode"):
        raw += sorted(globber(pattern))
    out: list[str] = []
    seen: set[str] = set()
    for p in raw:
        try:
            real = os.path.realpath(p)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        out.append(p)
    return out


def trusted_uids(config_path: str | None = None) -> set[int]:
    """root, le compte du process, et le propriétaire de ``config.yaml`` (il décide déjà d'``opencode_bin``)."""
    uids = {0}
    if hasattr(os, "geteuid"):
        uids.add(os.geteuid())
    try:
        uids.add(os.stat(get_config_path(config_path)).st_uid)
    except OSError:
        pass
    return uids


def is_trusted_binary(path: str, uids: set[int]) -> bool:
    """Fichier régulier exécutable, propriétaire de confiance, non modifiable par d'autres.

    Le propriétaire suffit (un fichier déposé par un tiers lui appartient) ; le mode ferme la
    substitution par un groupe ou le monde. Pas de règle d'emplacement : elle serait redondante.
    """
    try:
        real = os.path.realpath(path)
        st = os.stat(real)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode) or not (st.st_mode & 0o111):
        return False
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return st.st_uid in uids


def find_v2_binary(
    *,
    exclude: str | None = None,
    version_reader: Callable[[str], OpencodeVersion],
    config_path: str | None = None,
    home: str | None = None,
    path_env: str | None = None,
    globber: Callable[[str], list[str]] = glob.glob,
    candidates: list[str] | None = None,
) -> tuple[str, OpencodeVersion] | None:
    """Premier binaire opencode de la ligne ≥ 2 présent ET de confiance, ou ``None``.

    ``exclude`` : le binaire en cause (même fichier réel ⇒ ignoré). La version n'est lue que
    sur les candidats déjà jugés de confiance — on n'exécute pas un fichier douteux.
    ``candidates`` remplace la découverte (tests, écran : liste déjà établie).
    """
    uids = trusted_uids(config_path)
    excluded_real = os.path.realpath(exclude) if exclude else None
    if candidates is None:
        candidates = candidate_binaries(home=home, path_env=path_env, globber=globber)
    for candidate in candidates:
        if excluded_real and os.path.realpath(candidate) == excluded_real:
            continue
        if not is_trusted_binary(candidate, uids):
            continue
        try:
            version = version_reader(candidate)
        except Exception:  # noqa: BLE001 — un candidat qui ne répond pas n'est pas une panne
            continue
        if version.known and version.line >= 2:
            return candidate, version
    return None


# ── 3. basculer (disque + mémoire, audité, annulable) ────────────────────────

def _llm_block(config: dict | None) -> dict | None:
    if not isinstance(config, dict):
        return None
    wf = config.setdefault("workflow", {})
    if not isinstance(wf, dict):
        return None
    return wf.setdefault("arbitration_llm", {})


def _apply_in_memory(config: dict | None, values: Mapping[str, object]) -> None:
    """Pose les clés ``workflow.arbitration_llm.*`` dans ``config`` ET dans le singleton."""
    targets = [config]
    try:
        live = get_config()
    except Exception:  # noqa: BLE001 — hors application (doctor, CLI) : pas de singleton
        live = None
    if live is not None and live is not config:
        targets.append(live)
    for target in targets:
        block = _llm_block(target)
        if block is None:
            continue
        for dotted, value in values.items():
            block[dotted.rsplit(".", 1)[1]] = value


def _audit(action: str, details: dict, *, actor_id: str | None, actor_username: str) -> None:
    # Différé : dépendance base de données optionnelle par topologie (doctor/CLI sans base,
    # worker sans contexte applicatif) — l'audit est best-effort, la bascule ne l'attend pas.
    try:
        from transcria.audit.store import AuditStore

        AuditStore.log(action, actor_id=actor_id, actor_username=actor_username,
                       target_type="config", target_label="opencode", details=details)
    except Exception as exc:  # noqa: BLE001
        logger.info("audit de la bascule opencode non enregistré (%s) — journal seul", exc)


def switch_binary(
    config: dict | None,
    new_bin: str,
    *,
    previous: str,
    reason: str,
    config_path: str | None = None,
    actor_id: str | None = None,
    actor_username: str = "system",
    automatic: bool = True,
) -> dict:
    """Bascule ``opencode_bin`` : disque (si possible), mémoire (toujours), audit (best-effort).

    Returns:
        L'enregistrement ``{"from", "to", "at", "reason", "automatic", "persisted"}``.
        ``persisted=False`` ⇒ config.yaml non modifiable (lecture seule, absent) : la bascule
        vaut jusqu'au redémarrage et le journal le dit.
    """
    at = datetime.now(UTC).isoformat(timespec="seconds")
    values = {_KEY_BIN: new_bin, _KEY_PREVIOUS: previous, _KEY_AT: at, _KEY_REASON: reason}
    persisted = True
    try:
        set_yaml_file_scalars(Path(get_config_path(config_path)), values)
    except (OSError, YamlTextEditError) as exc:
        persisted = False
        logger.warning("bascule opencode non persistée dans config.yaml (%s) — effective en mémoire "
                       "jusqu'au redémarrage", exc)
    _apply_in_memory(config, values)
    record = {"from": previous, "to": new_bin, "at": at, "reason": reason,
              "automatic": automatic, "persisted": persisted}
    logger.warning("opencode basculé %s : %s → %s (motif : %s)",
                   "automatiquement" if automatic else "par l'administrateur", previous, new_bin, reason)
    _audit("config_opencode_switch", record, actor_id=actor_id, actor_username=actor_username)
    return record


def auto_switch_for(
    config: dict | None,
    *,
    current_bin: str,
    current_version: OpencodeVersion,
    kind: str | None,
    version_reader: Callable[[str], OpencodeVersion],
    config_path: str | None = None,
) -> dict | None:
    """Remède automatique d'une panne reconnue : un binaire v2 de confiance s'il en existe un.

    Ne bascule QUE pour ``SDK_TOOL_CALL_INCOMPAT`` depuis une ligne v1 — la seule panne dont le
    remède est connu et mécanique. Rend l'enregistrement de bascule, ou ``None`` (rien trouvé,
    ou déjà en v2 : la cause reste nommée, l'écran propose l'installation).
    """
    if kind != SDK_TOOL_CALL_INCOMPAT or current_version.line >= 2:
        return None
    found = find_v2_binary(exclude=current_bin, version_reader=version_reader, config_path=config_path)
    if found is None:
        logger.warning("panne opencode reconnue (%s) mais aucun binaire v2 de confiance sur la machine "
                       "— installation proposée dans l'écran « LLM d'arbitrage »", kind)
        return None
    new_bin, version = found
    return switch_binary(config, new_bin, previous=current_bin, config_path=config_path,
                         reason=f"{kind} (v{_short(current_version)} → v{_short(version)})")


def pending_switch(config: dict | None) -> dict | None:
    """Bascule enregistrée (annulable), lue dans une configuration (disque ou mémoire)."""
    block = ((config or {}).get("workflow") or {}).get("arbitration_llm") or {}
    previous = str(block.get("opencode_bin_previous") or "").strip()
    if not previous:
        return None
    return {"from": previous, "to": str(block.get("opencode_bin") or ""),
            "at": str(block.get("opencode_bin_switched_at") or ""),
            "reason": str(block.get("opencode_bin_switch_reason") or "")}


def revert_switch(
    config: dict | None,
    *,
    config_path: str | None = None,
    actor_id: str | None = None,
    actor_username: str = "system",
) -> str | None:
    """Annule la bascule enregistrée : ``opencode_bin`` reprend sa valeur d'avant, la trace est effacée."""
    pending = pending_switch(config)
    if pending is None:
        return None
    values = {_KEY_BIN: pending["from"], _KEY_PREVIOUS: "", _KEY_AT: "", _KEY_REASON: ""}
    try:
        set_yaml_file_scalars(Path(get_config_path(config_path)), values)
    except (OSError, YamlTextEditError) as exc:
        logger.warning("annulation de la bascule opencode non persistée (%s)", exc)
    _apply_in_memory(config, values)
    logger.warning("bascule opencode annulée : %s ← %s", pending["from"], pending["to"])
    _audit("config_opencode_switch", {**pending, "reverted": True},
           actor_id=actor_id, actor_username=actor_username)
    return pending["from"]
