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

from dataclasses import dataclass

from transcria.cli_i18n import make_translator

# Plancher VRAM des phases LLM — même seuil que la SECTION 9-bis d'install.sh
# (sous le palier 8 Go ⇒ transcription brute, aucun modèle d'arbitrage).
LLM_VRAM_FLOOR_MB = 7500

EXPRESS_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        "profile": "Profil : tout-en-un (portail web + traitement sur cette machine)",
        "hw_gpus": "Matériel : {n} GPU NVIDIA, {gb} Go de VRAM au total",
        "hw_none": "Matériel : aucun GPU NVIDIA détecté — transcription CPU possible (moteur Kroko), phases LLM indisponibles",
        "db_pg": "Base de données : PostgreSQL local (rôle « transcria », mot de passe généré automatiquement)",
        "db_sqlite_no_psql": "Base de données : SQLite (psql introuvable — installez postgresql pour un usage en production)",
        "db_sqlite_no_admin": "Base de données : SQLite (ni root ni sudo : impossible de créer le rôle PostgreSQL)",
        "db_sqlite_no_server": ("Base de données : SQLite (PostgreSQL installé mais serveur injoignable — "
                                "démarrez le service postgresql puis relancez avec --postgres pour l'utiliser)"),
        "models_token": "Modèles : Cohere + pyannote (qualité de référence — token HF fourni)",
        "models_open": ("Modèles : whisper + Sortformer, sans token HF — la qualité de référence "
                        "(Cohere + pyannote) restera activable depuis la page Modèles"),
        "models_open_small_gpu": ("Modèles : Kroko (transcription CPU, sans token) + Sortformer — "
                                  "le GPU entier reste à la LLM d'arbitrage (palier 8 Go)"),
        "models_open_cpu": ("Modèles : Kroko (transcription CPU pure, sans token) + Sortformer sur "
                            "CPU — machine sans GPU"),
        "models_kept": "Modèles : configuration existante conservée (config.yaml déjà présent)",
        "llm_tier": "LLM d'arbitrage : llama.cpp, palier {tier} Go — {label}, téléchargé automatiquement",
        "llm_generic": "LLM d'arbitrage : llama.cpp, palier choisi selon la VRAM ({gb} Go), modèle téléchargé automatiquement",
        "llm_raw": "LLM d'arbitrage : aucun (VRAM insuffisante) — transcription brute, sans correction ni résumé LLM",
        "service_yes": "Service systemd : installé (utilisateur {user})",
        "service_no": "Service systemd : non installé (--no-service)",
        "admin": "Compte admin : créé à la PREMIÈRE visite du portail (une page demande identifiant + mot de passe)",
    },
    "en": {
        "profile": "Profile: all-in-one (web portal + processing on this machine)",
        "hw_gpus": "Hardware: {n} NVIDIA GPU(s), {gb} GB of VRAM in total",
        "hw_none": "Hardware: no NVIDIA GPU detected — CPU transcription available (Kroko engine), LLM phases unavailable",
        "db_pg": "Database: local PostgreSQL (role \"transcria\", auto-generated password)",
        "db_sqlite_no_psql": "Database: SQLite (psql not found — install postgresql for production use)",
        "db_sqlite_no_admin": "Database: SQLite (neither root nor sudo: cannot create the PostgreSQL role)",
        "db_sqlite_no_server": ("Database: SQLite (PostgreSQL installed but the server is unreachable — "
                                "start the postgresql service, then re-run with --postgres to use it)"),
        "models_token": "Models: Cohere + pyannote (reference quality — HF token provided)",
        "models_open": ("Models: whisper + Sortformer, no HF token — reference quality "
                        "(Cohere + pyannote) stays available later from the Models page"),
        "models_open_small_gpu": ("Models: Kroko (CPU transcription, no token) + Sortformer — "
                                  "the whole GPU stays free for the arbitration LLM (8 GB tier)"),
        "models_open_cpu": ("Models: Kroko (pure-CPU transcription, no token) + Sortformer on "
                            "CPU — machine without GPU"),
        "models_kept": "Models: existing configuration kept (config.yaml already present)",
        "llm_tier": "Arbitration LLM: llama.cpp, {tier} GB tier — {label}, downloaded automatically",
        "llm_generic": "Arbitration LLM: llama.cpp, tier picked from VRAM ({gb} GB), model downloaded automatically",
        "llm_raw": "Arbitration LLM: none (not enough VRAM) — raw transcription, no LLM correction/summary",
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


def _llm_line(t, *, gpu_count: int, total_vram_mb: int, gpu_sizes_csv: str) -> str | None:
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
) -> ExpressPlan:
    t = make_translator(EXPRESS_MESSAGES, locale=locale)
    # Leçon du 1er passage réel (2026-08-07, conteneur vierge) : psql présent + droits
    # admin ne suffisent PAS — sans serveur qui répond (pg_isready), le bootstrap du rôle
    # échoue en plein install. L'express ne choisit PG que s'il est joignable MAINTENANT.
    setup_pg = psql_available and can_admin_pg and pg_server_reachable
    open_models = (not has_hf_token) and (not config_exists)

    # Backends du duo sans token, selon le matériel (jamais en dur dans install.sh) :
    # - GPU ≥ 12 Go : whisper (GPU) + Sortformer — le confort classique ;
    # - GPU < 12 Go (palier 8) : Kroko (CPU) + Sortformer — whisper (~3 Go GPU)
    #   étoufferait la LLM d'arbitrage résidente (4,6 Go) sur une carte de 8 Go ;
    # - sans GPU : Kroko + Sortformer, tous deux sur CPU (E2E validé 2026-08-06).
    stt_backend, diar_backend = "", ""
    if open_models:
        small_gpu = 0 < total_vram_mb < 11500
        stt_backend = "kroko" if (gpu_count == 0 or small_gpu) else "whisper"
        diar_backend = "sortformer"

    lines: list[str] = [t("profile")]
    if gpu_count > 0:
        lines.append(t("hw_gpus", n=gpu_count, gb=round(total_vram_mb / 1024)))
    else:
        lines.append(t("hw_none"))
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
    llm = _llm_line(t, gpu_count=gpu_count, total_vram_mb=total_vram_mb, gpu_sizes_csv=gpu_sizes_csv)
    if llm:
        lines.append(llm)
    lines.append(t("service_yes", user=service_user) if install_service else t("service_no"))
    lines.append(t("admin"))
    return ExpressPlan(setup_pg=setup_pg, open_models=open_models,
                       stt_backend=stt_backend, diarization_backend=diar_backend,
                       recap=tuple(lines))


def render_shell(plan: ExpressPlan) -> str:
    """Lignes machine consommées par install.sh (``eval_named_shell_assignments``)."""
    return (
        f"EXPRESS_SETUP_PG={'true' if plan.setup_pg else 'false'}\n"
        f"EXPRESS_OPEN_MODELS={'true' if plan.open_models else 'false'}\n"
        f"EXPRESS_STT_BACKEND={plan.stt_backend}\n"
        f"EXPRESS_DIAR_BACKEND={plan.diarization_backend}"
    )
