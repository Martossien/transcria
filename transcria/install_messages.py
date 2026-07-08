"""Catalogue FR/EN des messages de l'installateur Python (cf. transcria/cli_i18n.py).

Partagé par les modules ``transcria/install_*.py`` et ``transcria/installer/*`` : leur
sortie destinée à l'humain (rendue par ``install.sh`` via ``emit_rendered_log`` ou par les
phases) est localisée ici. Les valeurs ``fr`` reprennent MOT POUR MOT les libellés
historiques → sortie française octet-pour-octet inchangée (les tests tournent sans
``TRANSCRIA_DEFAULT_LOCALE`` = ``fr``). Le PRÉFIXE (``OK:``/``INFO:``/``WARN:``/``ERROR:``)
reste hors catalogue : il est ajouté par les modules et lu par ``install.sh`` (ne pas
localiser). Gabarits ``str.format`` (``{x}``).
"""
from __future__ import annotations

from transcria.cli_i18n import make_translator

INSTALL_MESSAGES: dict[str, dict[str, str]] = {
    "fr": {
        # install_paths.render_setup_log
        "path_venv_existing": "Venv existant : {value}",
        "path_venv_create_start": "Création du venv...",
        "path_venv_created": "Venv créé : {value}",
        "path_pip_upgrade": "Mise à jour de pip...",
        "path_requirements_start": "Installation requirements.txt...",
        "path_requirements_ok": "requirements.txt installé",
        "path_runtime_dirs_ready": "jobs/, models/, instance/ prêts",
        # install_prerequisites.render_setup_log
        "pre_python_ok": "Python {value} : {path}",
        "pre_python_missing": "Python 3.11+ requis. Installer avec: apt install python3.11",
        "pre_venv_missing": "module venv/ensurepip indisponible — `python -m venv` échouerait. "
                            "Installer avec: apt install python3-venv",
        "pre_nvidia_ok": "nvidia-smi — {value} GPU(s), CUDA {path}",
        "pre_nvidia_missing": "nvidia-smi non trouvé ou inutilisable — fonctionnement sans GPU "
                              "(transcription très lente)",
        "pre_binary_ok": "{name} : {path}",
        "pre_binary_req_ffmpeg": "{name} manquant. Installer avec: apt install ffmpeg",
        "pre_binary_req_generic": "{name} manquant.",
        "pre_binary_opt_lsof": "lsof manquant — requis par start.sh/stop.sh. Installer: apt install lsof",
        "pre_binary_opt_curl": "curl manquant — requis pour télécharger opencode (LLM d'arbitrage). "
                               "Installer: apt install curl",
        "pre_binary_opt_generic": "{name} manquant",
        # install_profiles : résumé final + commandes de démarrage (lignes en prose ;
        # les commandes systemctl / URLs / ports restent littérales, non localisées).
        "prof_rn_header": "TranscrIA Inference Service (nœud de ressources GPU)",
        "prof_rn_engines": "  Moteurs : diarize, voice-embed, STT (si déclarés dans config.yaml)",
        "prof_web_header": "TranscrIA tier web installé dans : {dir}",
        "prof_web_role": "  Rôle : web (Gunicorn, sans scheduler)",
        "prof_sched_header": "TranscrIA scheduler installé dans : {dir}",
        "prof_sched_role": "  Rôle : scheduler (ordonnanceur unique)",
        "prof_migrate_header": "TranscrIA migrations installées dans : {dir}",
        "prof_migrate_role": "  Rôle : migration Alembic uniquement",
        "prof_default_header": "TranscrIA installé dans : {dir}",
        "prof_next_rn": "Lancer le nœud de ressources :",
        "prof_next_web": "Lancer la frontale web :",
        "prof_next_sched": "Lancer l'ordonnanceur :",
        "prof_next_migrate": "Lancer les migrations :",
        "prof_next_default": "Lancer TranscrIA :",
        "prof_next_default_comment": "  # ou : sudo systemctl start transcria",
    },
    "en": {
        "path_venv_existing": "Existing venv: {value}",
        "path_venv_create_start": "Creating venv...",
        "path_venv_created": "Venv created: {value}",
        "path_pip_upgrade": "Upgrading pip...",
        "path_requirements_start": "Installing requirements.txt...",
        "path_requirements_ok": "requirements.txt installed",
        "path_runtime_dirs_ready": "jobs/, models/, instance/ ready",
        "pre_python_ok": "Python {value}: {path}",
        "pre_python_missing": "Python 3.11+ required. Install with: apt install python3.11",
        "pre_venv_missing": "venv/ensurepip module unavailable — `python -m venv` would fail. "
                            "Install with: apt install python3-venv",
        "pre_nvidia_ok": "nvidia-smi — {value} GPU(s), CUDA {path}",
        "pre_nvidia_missing": "nvidia-smi not found or unusable — running without GPU "
                              "(very slow transcription)",
        "pre_binary_ok": "{name}: {path}",
        "pre_binary_req_ffmpeg": "{name} missing. Install with: apt install ffmpeg",
        "pre_binary_req_generic": "{name} missing.",
        "pre_binary_opt_lsof": "lsof missing — required by start.sh/stop.sh. Install: apt install lsof",
        "pre_binary_opt_curl": "curl missing — required to download opencode (arbitration LLM). "
                               "Install: apt install curl",
        "pre_binary_opt_generic": "{name} missing",
        "prof_rn_header": "TranscrIA Inference Service (GPU resource node)",
        "prof_rn_engines": "  Engines: diarize, voice-embed, STT (if declared in config.yaml)",
        "prof_web_header": "TranscrIA web tier installed in: {dir}",
        "prof_web_role": "  Role: web (Gunicorn, no scheduler)",
        "prof_sched_header": "TranscrIA scheduler installed in: {dir}",
        "prof_sched_role": "  Role: scheduler (single scheduler)",
        "prof_migrate_header": "TranscrIA migrations installed in: {dir}",
        "prof_migrate_role": "  Role: Alembic migration only",
        "prof_default_header": "TranscrIA installed in: {dir}",
        "prof_next_rn": "Start the resource node:",
        "prof_next_web": "Start the web front-end:",
        "prof_next_sched": "Start the scheduler:",
        "prof_next_migrate": "Run the migrations:",
        "prof_next_default": "Start TranscrIA:",
        "prof_next_default_comment": "  # or: sudo systemctl start transcria",
    },
}

# Traducteur partagé (locale résolue depuis l'env à l'import ; chaque sous-process
# installateur l'importe frais → locale correcte car install.sh exporte l'env).
t = make_translator(INSTALL_MESSAGES)
