"""Mode express de install.sh — décisions par défaut + récapitulatif unique.

Constat (analyse installation 2026-08-06) : le chemin interactif posait ~10 questions
(PostgreSQL ×4, mot de passe admin, Cohere, token HF, backend LLM, palier, répertoires…)
— raisonnable pour un opérateur, intimidant pour un utilisateur lambda. Ce module
DÉCIDE les défauts depuis les détections déjà faites (matériel, psql, sudo, token HF)
et rédige le récapitulatif « voilà ce que je vais faire » ; install.sh l'affiche, pose
UNE confirmation, puis déroule le chemin non-interactif existant (qui prend ces mêmes
défauts partout). Le pas-à-pas historique reste disponible via ``--expert``.

Contrainte : appelé PRÉ-VENV par PYTHON_BIN (python système) → stdlib uniquement.
L'enrichissement « palier + nom du modèle LLM » passe par PyYAML : best-effort,
libellé générique si le python système ne l'a pas.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from transcria.cli_i18n import make_translator

# Repli CONSERVATEUR quand l'empreinte Whisper ne peut pas être lue (python système sans
# PyYAML) : classe large-v3 — mieux vaut Kroko CPU qu'une attente de VRAM sans fin.
WHISPER_FALLBACK_VRAM_MB = 10000

# Plancher VRAM des phases LLM — même seuil que la SECTION 9-bis d'install.sh
# (sous le palier 8 Go ⇒ transcription brute, aucun modèle d'arbitrage).
LLM_VRAM_FLOOR_MB = 7500

EXPRESS_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "profile": "Profil : tout-en-un (portail web + traitement sur cette machine)",
        "hw_gpus": "Matériel : {n} GPU NVIDIA, {gb} Go de VRAM au total",
        "hw_none": ("Matériel : aucun GPU NVIDIA détecté — transcription CPU possible (moteur Kroko), "
                    "phases LLM indisponibles sans fournisseur cloud"),
        "db_pg": "Base de données : PostgreSQL local (rôle « transcria », mot de passe généré automatiquement)",
        "db_sqlite_no_psql": "Base de données : SQLite (psql introuvable — installez postgresql pour un usage en production)",
        "db_sqlite_no_admin": "Base de données : SQLite (ni root ni sudo : impossible de créer le rôle PostgreSQL)",
        "db_sqlite_no_server": ("Base de données : SQLite (PostgreSQL installé mais serveur injoignable — "
                                "démarrez le service postgresql puis relancez avec --postgres pour l'utiliser)"),
        "models_token": ("Modèles : Cohere (STT de référence — token HF fourni) + diarisation « auto » "
                         "(Nemotron 3 de 1 à 8 voix, pyannote au-delà ou pour un comptage exact)"),
        "models_open": ("Modèles : whisper + diarisation « auto » (Nemotron 3, 1 à 8 voix), sans token HF — "
                        "le STT de référence (Cohere) restera activable depuis la page Modèles"),
        "models_open_small_gpu": ("Modèles : Kroko (transcription CPU, sans token) + diarisation « auto » "
                                  "(Nemotron 3, CPU) — le GPU entier reste à la LLM d'arbitrage (palier 8 Go)"),
        "models_open_cpu": ("Modèles : Kroko (transcription CPU pure, sans token) + diarisation « auto » "
                            "(Nemotron 3) sur CPU — machine sans GPU"),
        "models_kept": "Modèles : configuration existante conservée (config.yaml déjà présent)",
        "llm_tier": "LLM d'arbitrage : llama.cpp, palier {tier} Go — {label}, téléchargé automatiquement",
        "llm_generic": "LLM d'arbitrage : llama.cpp, palier choisi selon la VRAM ({gb} Go), modèle téléchargé automatiquement",
        "llm_raw": "LLM d'arbitrage : aucun (VRAM insuffisante) — transcription brute, sans correction ni résumé LLM",
        "hw_none_cloud": ("Matériel : aucun GPU NVIDIA détecté — palier « zéro GPU » : transcription CPU (Kroko) "
                          "+ diarisation Nemotron 3 CPU, phases LLM chez le fournisseur cloud"),
        "llm_cloud": ("LLM d'arbitrage : fournisseur cloud{provider} — aucun llama-server, aucun modèle "
                      "téléchargé, aucune VRAM réservée ; clé demandée après opencode (jamais dans config.yaml)"),
        "hw_none_cloud_kept": ("Matériel : aucun GPU NVIDIA détecté — palier « zéro GPU » : phases LLM chez "
                               "le fournisseur cloud, transcription selon la configuration conservée"),
        "llm_cloud_kept": ("LLM d'arbitrage : fournisseur cloud déjà configuré (config.yaml conservée) — "
                           "aucun llama-server ; relancer avec --llm-cloud pour en changer"),
        "llm_cloud_validation": ("Validation : un admin connecté devra autoriser l'envoi des transcriptions "
                                 "(Administration → LLM cloud) avant tout traitement LLM"),
        "service_yes": "Service systemd : installé (utilisateur {user})",
        "service_no": "Service systemd : non installé (--no-service)",
        "admin": "Compte admin : créé à la PREMIÈRE visite du portail (une page demande identifiant + mot de passe)",
    },
    "en": {
        "profile": "Profile: all-in-one (web portal + processing on this machine)",
        "hw_gpus": "Hardware: {n} NVIDIA GPU(s), {gb} GB of VRAM in total",
        "hw_none": ("Hardware: no NVIDIA GPU detected — CPU transcription available (Kroko engine), "
                    "LLM phases unavailable without a cloud provider"),
        "db_pg": "Database: local PostgreSQL (role \"transcria\", auto-generated password)",
        "db_sqlite_no_psql": "Database: SQLite (psql not found — install postgresql for production use)",
        "db_sqlite_no_admin": "Database: SQLite (neither root nor sudo: cannot create the PostgreSQL role)",
        "db_sqlite_no_server": ("Database: SQLite (PostgreSQL installed but the server is unreachable — "
                                "start the postgresql service, then re-run with --postgres to use it)"),
        "models_token": ("Models: Cohere (reference quality STT — HF token provided) + “auto” diarization "
                         "(Nemotron 3 for 1 to 8 voices, pyannote above or for an exact count)"),
        "models_open": ("Models: whisper + “auto” diarization (Nemotron 3, 1 to 8 voices), no HF token — "
                        "the reference STT (Cohere) stays available later from the Models page"),
        "models_open_small_gpu": ("Models: Kroko (CPU transcription, no token) + “auto” diarization "
                                  "(Nemotron 3, CPU) — the whole GPU stays free for the arbitration LLM (8 GB tier)"),
        "models_open_cpu": ("Models: Kroko (pure-CPU transcription, no token) + “auto” diarization "
                            "(Nemotron 3) on CPU — machine without GPU"),
        "models_kept": "Models: existing configuration kept (config.yaml already present)",
        "llm_tier": "Arbitration LLM: llama.cpp, {tier} GB tier — {label}, downloaded automatically",
        "llm_generic": "Arbitration LLM: llama.cpp, tier picked from VRAM ({gb} GB), model downloaded automatically",
        "llm_raw": "Arbitration LLM: none (not enough VRAM) — raw transcription, no LLM correction/summary",
        "hw_none_cloud": ("Hardware: no NVIDIA GPU detected — “zero GPU” tier: CPU transcription (Kroko) "
                          "+ Nemotron 3 CPU diarization, LLM phases at the cloud provider"),
        "llm_cloud": ("Arbitration LLM: cloud provider{provider} — no llama-server, no model download, "
                      "no VRAM reserved; key asked after opencode (never in config.yaml)"),
        "hw_none_cloud_kept": ("Hardware: no NVIDIA GPU detected — “zero GPU” tier: LLM phases at the cloud "
                               "provider, transcription as per the kept configuration"),
        "llm_cloud_kept": ("Arbitration LLM: cloud provider already configured (config.yaml kept) — "
                           "no llama-server; re-run with --llm-cloud to change it"),
        "llm_cloud_validation": ("Approval: a signed-in admin must allow sending transcripts "
                                 "(Administration → Cloud LLM) before any LLM processing"),
        "service_yes": "systemd service: installed (user {user})",
        "service_no": "systemd service: not installed (--no-service)",
        "admin": "Admin account: created on the portal's FIRST visit (a page asks for username + password)",
    },
}


@dataclass(frozen=True)
class ExpressPlan:
    """Décisions du mode express + récapitulatif prêt à afficher.

    ``open_models`` encode déjà « config fraîche ET sans token » : install.sh n'a
    pas à re-vérifier la fraîcheur au moment d'écrire les backends. ``stt_backend``/
    ``diarization_backend`` portent le CHOIX (décidé ici, jamais en dur dans le
    shell) : whisper + sortformer avec GPU, kroko (CPU pur) sans GPU."""

    setup_pg: bool
    open_models: bool
    stt_backend: str
    diarization_backend: str
    recap: tuple[str, ...]
    llm_cloud: bool = False


_SERVICES_RE = re.compile(r"^services\s*:\s*(#.*)?$")
_BACKEND_RE = re.compile(r"^\s+backend\s*:\s*['\"]?([A-Za-z_-]+)['\"]?\s*(#.*)?$")


def config_declares_cloud_backend(config_path: Path) -> bool:
    """``services.backend: cloud`` dans une config existante — lecture TEXTE (pré-venv, sans PyYAML).

    Ne regarde que la clé ``backend`` directement indentée sous la section racine ``services:``
    (un ``backend`` d'une autre section, p. ex. ``auth.backend``, ne compte pas)."""
    try:
        lines = Path(config_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    in_services = False
    for line in lines:
        if line and not line[0].isspace() and not line.lstrip().startswith("#"):
            in_services = bool(_SERVICES_RE.match(line))
            continue
        if in_services:
            m = _BACKEND_RE.match(line)
            if m and len(line) - len(line.lstrip()) == 2:
                return m.group(1).strip().lower() == "cloud"
    return False


def whisper_required_vram_mb() -> int:
    """VRAM requise par le Whisper d'une config FRAÎCHE (l'express ne décide que sur config neuve).

    Source unique : le descripteur du backend (``whisper_transcriber.vram_mb`` →
    ``_MODEL_VRAM`` selon le ``model_size`` par défaut). Best-effort : repli conservateur."""
    try:
        # Différé et best-effort : tire PyYAML (config par défaut), absent du python système.
        from transcria.stt.whisper_transcriber import vram_mb as whisper_vram_mb

        return int(whisper_vram_mb({}))
    except Exception:  # noqa: BLE001 — PyYAML absent / module illisible : repli conservateur
        return WHISPER_FALLBACK_VRAM_MB


def _largest_card_mb(gpu_count: int, total_vram_mb: int, gpu_sizes_csv: str) -> int:
    sizes = [int(x) for x in gpu_sizes_csv.split(",") if x.strip().isdigit()]
    if sizes:
        return max(sizes)
    return total_vram_mb // gpu_count if gpu_count > 0 else 0


def _llm_line(t, *, gpu_count: int, total_vram_mb: int, gpu_sizes_csv: str,
              llm_cloud: bool = False, cloud_provider: str = "", cloud_kept: bool = False) -> str | None:
    if llm_cloud and cloud_kept:
        return t("llm_cloud_kept")
    if llm_cloud:
        # Palier « zéro GPU » (docs/LLM_CLOUD.md § 4) : ni palier, ni GGUF, ni calibration.
        return t("llm_cloud", provider=f" {cloud_provider}" if cloud_provider else "")
    if gpu_count <= 0:
        return None  # « hw_none » a déjà dit que les phases LLM sont indisponibles
    if total_vram_mb < LLM_VRAM_FLOOR_MB:
        return t("llm_raw")
    try:
        # Best-effort : recommandation par PLACEMENT réel (même logique que la
        # SECTION 9-bis) — tire PyYAML, absent de certains python système.
        from transcria.installer.arbitrage import recommend_placement_tier
        from transcria.installer.tiers import get_tier_metadata

        rec = recommend_placement_tier(gpu_sizes_csv=gpu_sizes_csv, total_vram_mb=total_vram_mb)
        if not rec.tier:
            return t("llm_raw")
        return t("llm_tier", tier=rec.tier, label=get_tier_metadata(rec.tier).label)
    except Exception:  # noqa: BLE001 — PyYAML absent / catalogue illisible : libellé générique
        return t("llm_generic", gb=round(total_vram_mb / 1024))


def build_express_plan(
    *,
    gpu_count: int,
    total_vram_mb: int,
    gpu_sizes_csv: str = "",
    psql_available: bool,
    can_admin_pg: bool,
    pg_server_reachable: bool,
    has_hf_token: bool,
    config_exists: bool,
    install_service: bool,
    service_user: str,
    locale: str | None = None,
    llm_cloud: bool = False,
    cloud_provider: str = "",
    cloud_already_configured: bool = False,
) -> ExpressPlan:
    """Décisions + récapitulatif. ``llm_cloud`` = ``--llm-cloud`` demandé OU config déjà en cloud ;
    ``cloud_already_configured`` distingue le second cas (rien ne sera redemandé)."""
    t = make_translator(EXPRESS_MESSAGES, locale=locale)
    # Leçon du 1er passage réel (2026-08-07, conteneur vierge) : psql présent + droits
    # admin ne suffisent PAS — sans serveur qui répond (pg_isready), le bootstrap du rôle
    # échoue en plein install. L'express ne choisit PG que s'il est joignable MAINTENANT.
    setup_pg = psql_available and can_admin_pg and pg_server_reachable
    open_models = (not has_hf_token) and (not config_exists)

    # Backends du duo sans token, selon le matériel (jamais en dur dans install.sh) :
    # - GPU ≥ 12 Go : whisper (GPU) — le confort classique ;
    # - GPU < 12 Go (palier 8) : Kroko (CPU) — whisper (~3 Go GPU) étoufferait la LLM
    #   d'arbitrage résidente (4,6 Go) sur une carte de 8 Go ;
    # - sans GPU : Kroko sur CPU (E2E validé 2026-08-06) — avec --llm-cloud, c'est le palier
    #   « zéro GPU » complet (LLM chez le fournisseur, docs/LLM_CLOUD.md § 4).
    # Diarisation : « auto » partout depuis 0.4.7 — Nemotron 3 (non gated, CPU à l'upload)
    # de 1 à 8 voix, pyannote au-delà ; Sortformer (≤ 4) n'est plus le défaut sans token.
    stt_backend, diar_backend = "", ""
    if open_models:
        if llm_cloud:
            # Aucune LLM résidente : whisper dès que la PLUS GRANDE carte loge le modèle Whisper
            # configuré (large-v3 ≈ 10 Go) ; sinon Kroko CPU (jamais d'attente VRAM sans fin).
            required = whisper_required_vram_mb() if gpu_count > 0 else 0
            whisper_fits = gpu_count > 0 and _largest_card_mb(gpu_count, total_vram_mb, gpu_sizes_csv) >= required
            stt_backend = "whisper" if whisper_fits else "kroko"
        else:
            small_gpu = 0 < total_vram_mb < 11500
            stt_backend = "kroko" if (gpu_count == 0 or small_gpu) else "whisper"
        diar_backend = "auto"

    lines: list[str] = [t("profile")]
    if gpu_count > 0:
        lines.append(t("hw_gpus", n=gpu_count, gb=round(total_vram_mb / 1024)))
    else:
        if not llm_cloud:
            lines.append(t("hw_none"))
        elif stt_backend == "kroko":  # le duo Kroko + Nemotron CPU est réellement écrit
            lines.append(t("hw_none_cloud"))
        else:
            lines.append(t("hw_none_cloud_kept"))
    if setup_pg:
        lines.append(t("db_pg"))
    elif not psql_available:
        lines.append(t("db_sqlite_no_psql"))
    elif not can_admin_pg:
        lines.append(t("db_sqlite_no_admin"))
    else:
        lines.append(t("db_sqlite_no_server"))
    if config_exists:
        lines.append(t("models_kept"))
    elif has_hf_token:
        lines.append(t("models_token"))
    elif stt_backend == "kroko":
        lines.append(t("models_open_cpu") if gpu_count == 0 else t("models_open_small_gpu"))
    else:
        lines.append(t("models_open"))
    llm = _llm_line(t, gpu_count=gpu_count, total_vram_mb=total_vram_mb, gpu_sizes_csv=gpu_sizes_csv,
                    llm_cloud=llm_cloud, cloud_provider=cloud_provider,
                    cloud_kept=cloud_already_configured)
    if llm:
        lines.append(llm)
    if llm_cloud:
        lines.append(t("llm_cloud_validation"))
    lines.append(t("service_yes", user=service_user) if install_service else t("service_no"))
    lines.append(t("admin"))
    return ExpressPlan(setup_pg=setup_pg, open_models=open_models,
                       stt_backend=stt_backend, diarization_backend=diar_backend,
                       recap=tuple(lines), llm_cloud=llm_cloud)


def render_shell(plan: ExpressPlan) -> str:
    """Lignes machine consommées par install.sh (``eval_named_shell_assignments``)."""
    return (
        f"EXPRESS_SETUP_PG={'true' if plan.setup_pg else 'false'}\n"
        f"EXPRESS_OPEN_MODELS={'true' if plan.open_models else 'false'}\n"
        f"EXPRESS_STT_BACKEND={plan.stt_backend}\n"
        f"EXPRESS_DIAR_BACKEND={plan.diarization_backend}\n"
        f"EXPRESS_LLM_CLOUD={'true' if plan.llm_cloud else 'false'}"
    )
