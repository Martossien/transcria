"""Détection de nouvelle version via l'API GitHub Releases (opt-in).

Le portail est auto-hébergé : AUCUN appel réseau sortant sans opt-in explicite
(``maintenance.update_check.enabled``) ou action manuelle de l'opérateur (bouton
« Vérifier maintenant » de la page Maintenance). Rien n'est transmis hors la
requête HTTPS elle-même (même exposition qu'un ``git fetch``). La réponse est
mise en cache (JSON dans le dossier de sauvegarde) pour ne pas marteler l'API
GitHub : au plus un appel par ``CACHE_TTL_S`` en mode automatique.

Logique pure et testable : le fetcher HTTP et l'horloge sont injectables.
"""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from transcria import __version__

GITHUB_REPO = "Martossien/transcria"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
CACHE_FILENAME = "update-check.json"
CACHE_TTL_S = 24 * 3600          # mode automatique : au plus un appel par jour
_FETCH_TIMEOUT_S = 4.0           # la page admin ne doit jamais attendre longtemps
_NOTES_MAX_CHARS = 2000
_NOTES_MAX_LINES = 30

FetchFn = Callable[[str], dict]


class UpdateCheckError(Exception):
    """Échec de la vérification (réseau, API, réponse inattendue) — message actionnable."""


def current_version() -> str:
    """Version installée — ce module est l'unique porte d'entrée « version » des routes web."""
    return __version__


def parse_version(text: str) -> tuple[tuple[int, ...], bool] | None:
    """Analyse ``v0.4.0`` / ``0.3.9.1`` / ``v0.1.0-beta.8`` → (composants, est_finale).

    Retourne ``None`` si le texte ne commence pas par un numéro exploitable.
    Une pré-version (suffixe ``-…``) est ANTÉRIEURE à la version finale de même numéro.
    """
    body = text.strip().lstrip("vV")
    numeric, _, suffix = body.partition("-")
    parts = numeric.split(".")
    try:
        components = tuple(int(p) for p in parts)
    except ValueError:
        return None
    if not components:
        return None
    return components, not suffix


def is_newer(candidate: str, reference: str) -> bool:
    """``candidate`` est-elle STRICTEMENT plus récente que ``reference`` ?

    Prudence sur l'inanalysable : ``False`` (on ne signale jamais une « mise à
    jour » qu'on ne sait pas comparer).
    """
    parsed_candidate = parse_version(candidate)
    parsed_reference = parse_version(reference)
    if parsed_candidate is None or parsed_reference is None:
        return False
    nums_c, final_c = parsed_candidate
    nums_r, final_r = parsed_reference
    width = max(len(nums_c), len(nums_r))
    padded_c = nums_c + (0,) * (width - len(nums_c))
    padded_r = nums_r + (0,) * (width - len(nums_r))
    # La finale bat la pré-version de même numéro (0.4.0 > 0.4.0-beta.1).
    return (padded_c, final_c) > (padded_r, final_r)


def cache_path(cfg: dict) -> Path:
    """Fichier de cache, rangé avec l'état maintenance (dossier de sauvegarde)."""
    backup_dir = (cfg.get("maintenance", {}) or {}).get("backup_dir") or "./backups"
    return Path(backup_dir) / CACHE_FILENAME


def _default_fetch(url: str) -> dict:
    import requests

    response = requests.get(
        url,
        timeout=_FETCH_TIMEOUT_S,
        headers={"Accept": "application/vnd.github+json",
                 "User-Agent": "transcria-update-check"},
    )
    if response.status_code != 200:
        raise UpdateCheckError(
            f"l'API GitHub a répondu {response.status_code} — réessayez plus tard "
            f"(limite de débit possible) ou consultez {RELEASES_PAGE}")
    return response.json()


def _notes_excerpt(body: str) -> str:
    lines = (body or "").splitlines()[:_NOTES_MAX_LINES]
    return "\n".join(lines)[:_NOTES_MAX_CHARS].strip()


def fetch_latest_release(fetch: FetchFn | None = None) -> dict:
    """Interroge ``releases/latest`` (ni brouillon ni pré-version, par contrat GitHub).

    Retourne un enregistrement NORMALISÉ : ``tag``, ``url``, ``published_at``, ``notes``.
    """
    try:
        payload = (fetch or _default_fetch)(_API_URL)
    except UpdateCheckError:
        raise
    except Exception as exc:  # noqa: BLE001 — réseau coupé, DNS, TLS… → message unique
        raise UpdateCheckError(
            f"impossible de joindre l'API GitHub ({exc}) — vérifiez l'accès réseau "
            f"sortant ou consultez {RELEASES_PAGE}") from exc
    tag = str(payload.get("tag_name") or "").strip()
    if parse_version(tag) is None:
        raise UpdateCheckError(f"réponse GitHub inattendue (tag « {tag or '∅'} »)")
    return {
        "tag": tag,
        "url": str(payload.get("html_url") or RELEASES_PAGE),
        "published_at": str(payload.get("published_at") or ""),
        "notes": _notes_excerpt(str(payload.get("body") or "")),
    }


def read_cache(path: Path) -> dict | None:
    """Cache si lisible et bien formé, sinon ``None`` (jamais d'exception)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or parse_version(str(data.get("tag") or "")) is None:
        return None
    return data


def is_stale(cached: dict | None, *, now_fn: Callable[[], float] = time.time) -> bool:
    if not cached:
        return True
    try:
        checked = datetime.fromisoformat(str(cached.get("checked_at")))
    except (TypeError, ValueError):
        return True
    return (now_fn() - checked.timestamp()) > CACHE_TTL_S


def check_for_update(
    cfg: dict,
    *,
    fetch: FetchFn | None = None,
    now_fn: Callable[[], float] = time.time,
) -> dict:
    """Interroge GitHub et écrit le cache. Lève ``UpdateCheckError`` en cas d'échec.

    L'appelant décide QUAND appeler (bouton manuel, ou cache périmé en mode
    automatique) — ici on fait toujours l'appel réseau.
    """
    release = fetch_latest_release(fetch)
    release["checked_at"] = datetime.fromtimestamp(now_fn(), tz=UTC).isoformat()
    path = cache_path(cfg)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(release, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # un cache inécrivable ne doit pas masquer le résultat de la vérification
    return release


def summarize(cached: dict | None, current_version: str) -> dict:
    """Vue pour l'UI : compare le cache (éventuel) à la version COURANTE.

    ``newer`` est recalculé à chaque lecture — jamais stocké — pour rester juste
    après une mise à niveau (le cache peut dater d'avant).
    """
    view = {
        "current": current_version,
        "releases_page": RELEASES_PAGE,
        "checked_at": None,
        "tag": None,
        "url": None,
        "published_at": None,
        "notes": "",
        "newer": False,
    }
    if cached:
        view.update({
            "checked_at": cached.get("checked_at"),
            "tag": cached.get("tag"),
            "url": cached.get("url") or RELEASES_PAGE,
            "published_at": cached.get("published_at"),
            "notes": str(cached.get("notes") or ""),
            "newer": is_newer(str(cached.get("tag") or ""), current_version),
        })
    return view
