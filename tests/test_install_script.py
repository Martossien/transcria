"""Contrats sans effet de bord de `install.sh`.

Ces tests n'exécutent que `--plan` ou des validations d'arguments qui sortent avant
toute création de venv/config/.env/service. Ils verrouillent les décisions de profil
sans lancer l'installation réelle.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.install_profiles import resolve_install_plan

_ROOT = Path(__file__).resolve().parents[1]
_INSTALL = _ROOT / "install.sh"


def _run_install(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(_INSTALL), *args],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _parse_plan(stdout: str) -> dict[str, object]:
    values: dict[str, object] = {}
    units: list[str] = []
    in_units = False
    for raw in stdout.splitlines():
        line = raw.rstrip()
        if line == "systemd_units:":
            in_units = True
            continue
        if in_units:
            if line.startswith("  - "):
                value = line.removeprefix("  - ")
                if value != "none":
                    units.append(value)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            values[key] = {"true": True, "false": False}.get(value, value)
    values["systemd_units"] = units
    return values


def test_install_script_shell_syntax_is_valid():
    result = subprocess.run(
        ["bash", "-n", str(_INSTALL)],
        cwd=_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_install_script_does_not_eval_interactive_answers():
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'eval "$varname=' not in content
    assert 'printf -v "$varname"' in content


def test_install_script_resolves_user_home_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.install_prerequisites user-home" in content
    assert "pwd.getpwnam" not in content
    assert "python3 -c" not in content


def test_install_script_uses_run_indented_for_command_output_prefixing():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "run_indented()" in content
    assert "run_indented env TRANSCRIA_DATABASE_URL=" in content
    assert "run_indented env PYTHONPATH=" in content
    assert "2>&1 | sed 's/^/  /'" not in content
    assert "print_indented_file" in content
    assert "sed 's/^/  /'" not in content


def test_install_script_reuses_systemd_unit_installer_for_inference():
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'install_systemd_unit "$TMP_INF" "$INFERENCE_DST" "transcria-inference" "transcria-inference.service.adapted"' in content
    assert 'sudo cp "$TMP_INF" "$INFERENCE_DST"' not in content
    assert 'cp "$TMP_INF" "$INFERENCE_DST"' not in content


def test_install_script_reuses_split_systemd_unit_installer():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "install_deploy_unit()" in content
    assert '"transcria-migrate.service.adapted"' in content
    assert '"transcria-web.service.adapted"' in content
    assert '"transcria-scheduler.service.adapted"' in content
    assert "TMP_MIGRATE=" not in content
    assert "TMP_WEB=" not in content
    assert "TMP_SCHEDULER=" not in content


def test_install_script_supports_explicit_skip_doctor():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--skip-doctor" in content
    assert "SKIP_DOCTOR=true" in content
    assert 'DOCTOR_STATUS="sauté (--skip-doctor)"' in content


def test_install_script_supports_strict_doctor():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--strict-doctor" in content
    assert "STRICT_DOCTOR=true" in content
    assert 'DOCTOR_ARGS+=(--strict)' in content
    assert "--skip-doctor et --strict-doctor sont incompatibles" in content


def test_install_script_filters_llm_shell_helper_outputs_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "eval_prefixed_shell_assignments()" in content
    assert "eval_prefixed_shell_assignments LLM" in content
    assert "eval_prefixed_shell_assignments LLAMA" in content
    assert "grep -E '^LLM_[A-Z_]+='" not in content
    assert "grep -E '^LLAMA_[A-Z_]+='" not in content


def test_install_script_filters_first_available_helper_outputs_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "eval_prefixed_shell_assignments FIRST_AVAILABLE" in content
    assert 'eval "$HF_COHERE_OUT"' not in content
    assert 'eval "$LLAMA_FALLBACK_OUT"' not in content
    assert 'eval "$HF_DL_OUT"' not in content


def test_install_script_filters_named_helper_outputs_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "eval_named_shell_assignments" in content
    assert "HAVE_NVIDIA_SMI HAVE_RUNUSER HAVE_SERVICE HAVE_SUDO HAVE_SYSTEMCTL" in content
    assert "GPU_COUNT CUDA_VER_FROM_SMI NVIDIA_WARNING" in content
    assert "CUDA_TAG CUDA_WARNING" in content
    assert 'eval "$SYSTEM_CAPABILITIES_OUT"' not in content
    assert 'eval "$NVIDIA_DETECT_OUT"' not in content
    assert 'eval "$TORCH_TAG_OUT"' not in content


def test_install_script_filters_profile_plan_output_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "INSTALL_PROFILE INSTALL_RUNTIME_ROLE INSTALL_SERVICE INSTALL_INFERENCE SETUP_PG" in content
    assert "PROFILE_NEEDS_LOCAL_MODELS PROFILE_NEEDS_LLM PROFILE_NEEDS_ADMIN_CONFIG" in content
    assert 'eval "$plan_shell"' not in content


def test_install_script_uses_profile_renderer_for_final_text():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "print_profile_text summary" in content
    assert 'print_profile_text next-steps "$FINAL_LOG_FILE"' in content
    assert 'echo -e "${BOLD}Lancer le nœud de ressources' not in content


def test_install_script_uses_model_summary_renderer():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "print_model_summary" in content
    assert "-m transcria.install_models summary" in content
    assert 'echo -e "${BOLD}Modèles IA' not in content
    assert '$COHERE_OK  && echo -e' not in content


def test_install_script_uses_model_detection_table_renderer():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "print_model_detection_table" in content
    assert "-m transcria.install_models detection-table" in content
    assert "┌─────────────────────────────────" not in content
    assert 'printf "  │ %-31s' not in content


def test_install_script_delegates_model_status_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_models status-log" in content
    assert "log_model_status_event" in content
    assert "Cohere ASR       : $COHERE_PATH" not in content
    assert "pyannote cache   : $(basename" not in content
    assert "SQUIM préflight  : $SQUIM_PTH" not in content
    assert "LLM arbitrage    : $QWEN_GGUF" not in content
    assert "vérification des modèles GPU locaux sautée" not in content


def test_install_script_delegates_cohere_setup_logs_and_prompt():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_models cohere-setup-log" in content
    assert "-m transcria.install_models cohere-setup-prompt" in content
    assert "log_cohere_setup_event" in content
    assert "Le modèle Cohere ASR est introuvable" not in content
    assert "Chemin actuel dans config.yaml" not in content
    assert "cohere_model_path mis à jour" not in content
    assert "Chemin introuvable — config inchangée" not in content
    assert "Téléchargement de CohereLabs" not in content
    assert "Modèle Cohere téléchargé et configuré" not in content
    assert "Téléchargement échoué — vérifiez vos accès HuggingFace" not in content
    assert "huggingface-cli non trouvé" not in content
    assert "Modèle Cohere ignoré" not in content


def test_install_script_delegates_pyannote_setup_logs_and_prompts():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_models pyannote-setup-log" in content
    assert "-m transcria.install_models pyannote-token-prompt" in content
    assert "-m transcria.install_models pyannote-download-prompt" in content
    assert "log_pyannote_setup_event" in content
    assert "HF_TOKEN manquant — requis pour télécharger pyannote" not in content
    assert "https://huggingface.co/settings/tokens" not in content
    assert "Accepter les conditions" not in content
    assert "HF_TOKEN (laisser vide pour ignorer)" not in content
    assert "HF_TOKEN sauvegardé dans .env" not in content
    assert "Télécharger pyannote/speaker-diarization-community-1 maintenant" not in content
    assert "Téléchargement pyannote (peut prendre quelques minutes)" not in content
    assert "pyannote téléchargé" not in content
    assert "Téléchargement pyannote échoué" not in content
    assert "&& log_ok \"pyannote téléchargé\"" not in content


def test_install_script_delegates_opencode_setup_logs_and_prompt():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_opencode --setup-log" in content
    assert "-m transcria.install_opencode \\\n            --install-prompt" in content
    assert "log_opencode_setup_event" in content
    assert "opencode trouvé :" not in content
    assert "opencode non trouvé" not in content
    assert "Installer opencode dans $OPENCODE_HOME" not in content
    assert "Téléchargement opencode (linux-x64)" not in content
    assert "opencode installé :" not in content
    assert "PATH mis à jour dans" not in content
    assert "Relancez votre shell ou" not in content
    assert "Téléchargement opencode échoué" not in content
    assert "Installation manuelle :" not in content
    assert "mkdir -p ~/.opencode/bin" not in content
    assert "chmod +x ~/.opencode/bin/opencode" not in content
    assert "opencode ignoré — résumé/correction LLM désactivé" not in content
    assert "Pour installer plus tard" not in content
    assert "Configuration du provider opencode local" not in content
    assert "opencode provider local configuré" not in content
    assert "Configuration opencode incomplète" not in content
    assert "opencode non requis" not in content


def test_install_script_delegates_llm_selection_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_arbitrage --setup-log" in content
    assert "log_llm_setup_event" in content
    assert "LLM d'arbitrage locale non requise" not in content
    assert "VRAM totale ${GPU_VRAM_TOTAL_MB} Mio (< 12 Go)" not in content
    assert "TRANSCRIPTION BRUTE (résumé/correction LLM désactivés)" not in content
    assert "opencode absent — LLM d'arbitrage non configurable" not in content
    assert "Installez opencode puis relancez" not in content
    assert "VRAM : total ${GPU_VRAM_TOTAL_MB} Mio" not in content
    assert "Planner de placement indisponible" not in content
    assert "Aucun palier LLM ne tient" not in content
    assert "Palier recommandé :" not in content
    assert "Paliers : 12 / 16 / 24 / 32 / 48 / 64" not in content


def test_install_script_uses_final_status_renderers():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "print_database_summary" in content
    assert "print_configuration_summary" in content
    assert "-m transcria.install_summary database" in content
    assert "-m transcria.install_summary configuration" in content
    assert 'echo -e "${BOLD}Base de données' not in content
    assert 'echo -e "${BOLD}Configuration' not in content
    assert '[[ "$DB_BACKEND" == PostgreSQL* ]]' not in content


def test_install_script_delegates_configuration_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_summary setup-log" in content
    assert "log_config_setup_event" in content
    assert "config.yaml existant conservé" not in content
    assert "Ancien config.yaml sauvegardé" not in content
    assert "Génération via bootstrap_config.py" not in content
    assert "Clé secrète Flask générée" not in content
    assert "TRANSCRIA_SECRET présent" not in content
    assert "Profil d'installation : $INSTALL_PROFILE" not in content
    assert "TRANSCRIA_INFERENCE_API_KEY présent" not in content
    assert "Proxy déjà présent dans .env" not in content
    assert "Mot de passe admin : valeur par défaut" not in content
    assert "Mot de passe admin défini" not in content
    assert "Trop court — inchangé" not in content
    assert "config.yaml mis à jour" not in content
    assert ".env sécurisé pour l'utilisateur de service" not in content


def test_install_script_delegates_postgres_schema_action_decision():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--schema-action" in content
    assert "--schema-action-log" in content
    assert 'case "$schema_action" in' in content
    assert '[[ "$has_schema" -gt 0 && "${has_data:-0}" -gt 0 ]]' not in content
    assert "existe déjà avec des données. Conservation" not in content
    assert "Application des migrations Alembic" not in content


def test_install_script_delegates_postgres_alembic_result_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--alembic-log" in content
    assert "log_postgres_alembic_event" in content
    assert "Schéma à jour (Alembic)" not in content
    assert "Tentative de reconstruction locale" not in content
    assert "Schéma reconstruit" not in content
    assert "Reconstruction automatique refusée" not in content
    assert "Schéma PostgreSQL créé" not in content
    assert "Échec d'alembic upgrade head" not in content
    assert "Action Alembic PostgreSQL inconnue" not in content


def test_install_script_delegates_sqlite_migration_action_decision():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--sqlite-migration-action" in content
    assert 'case "$sqlite_migration_action" in' in content
    assert 'if [[ -s "$sqlite_db" && ( -z "$has_data" || "$has_data" -eq 0 ) ]]' not in content


def test_install_script_delegates_sqlite_migration_logs_and_prompt():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--sqlite-migration-log" in content
    assert "--sqlite-migration-prompt" in content
    assert "log_sqlite_migration_event" in content
    assert "Base SQLite détectée : $sqlite_db" not in content
    assert "Migration sautée (--pg-migrate absent)" not in content
    assert "Migration SQLite → PostgreSQL" not in content
    assert "  1. Migrer les données SQLite" not in content
    assert "Action de migration SQLite inconnue" not in content
    assert "log_sqlite_migrate_event" in content
    assert "Échec du backup SQLite" not in content
    assert "Backup SQLite sauvegardé" not in content
    assert "Migration des données SQLite → PostgreSQL" not in content
    assert "Données migrées" not in content
    assert "Échec de la migration SQLite" not in content
    assert "partiellement remplie" not in content


def test_install_script_delegates_database_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--database-setup-log" in content
    assert "log_database_setup_event" in content
    assert "Base SQLite conservée (storage.database_url de config.yaml)" not in content
    assert "psql introuvable" not in content
    assert "sudo requis pour créer le rôle" not in content
    assert "arrêt au lieu de poursuivre silencieusement en SQLite" not in content
    assert "Mot de passe du rôle '$PG_USER' généré automatiquement" not in content
    assert 'DB_BACKEND="PostgreSQL ($PG_DB@$PG_HOST:$PG_PORT)"' not in content
    assert "PostgreSQL demandé mais la configuration a échoué" not in content


def test_install_script_delegates_postgres_role_and_database_sql_rendering():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--role-sql" in content
    assert "--database-sql" in content
    assert "--fallback-locale-c" in content
    assert "CREATE ROLE %I LOGIN PASSWORD" not in content
    assert "CREATE DATABASE %I OWNER %I" not in content


def test_install_script_delegates_postgres_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--setup-log" in content
    assert "log_postgres_setup_event" in content
    assert "Vérification du rôle '$user' et de la base '$db'" not in content
    assert "Échec de la création du rôle PostgreSQL" not in content
    assert "CREATE DATABASE UTF8 refusé" not in content
    assert "Rôle et base PostgreSQL prêts" not in content
    assert "PostgreSQL distant détecté ($host)" not in content
    assert "Connexion PostgreSQL validée" not in content
    assert "DSN PostgreSQL écrit dans .env" not in content


def test_install_script_delegates_postgres_state_queries():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "pg_state_query" in content
    assert "--state-query" in content
    assert "--state-summary" in content
    assert "SELECT COUNT(*) FROM users" not in content
    assert "SELECT version_num FROM alembic_version" not in content
    assert "pg_encoding_to_char" not in content
    assert "tables public=$has_schema" not in content


def test_install_script_delegates_pg_hba_rewrite_result_parsing():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--pg-hba-rewrite-result" in content
    assert '"$pg_hba_result" =~ ^changed=' not in content
    assert '[[ "$pg_hba_result" != "changed=0" ]]' not in content


def test_install_script_delegates_postgres_encoding_warnings():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--encoding-warnings" in content
    assert "texte stocké SANS validation d'encodage" not in content
    assert "L'application force client_encoding=utf8" not in content


def test_install_script_delegates_postgres_connection_failure_messages():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--connection-failure" in content
    assert "Connexion PostgreSQL impossible avec le rôle" not in content
    assert "Créez la base et le rôle côté serveur" not in content


def test_install_script_uses_requirements_as_runtime_dependency_source():
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'pip install -r "$INSTALL_DIR/requirements.txt" --quiet' in content
    assert "pip install accelerate" not in content
    assert "pip install python-dotenv" not in content


def test_install_script_delegates_common_runtime_directories_to_python():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_paths" in content
    assert 'mkdir -p "$INSTALL_DIR/jobs" "$INSTALL_DIR/models/cohere-asr" "$INSTALL_DIR/instance"' not in content


def test_install_script_has_no_effective_mkdir_calls():
    content = _INSTALL.read_text(encoding="utf-8")

    effective_mkdir = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("mkdir -p") and not line.strip().startswith('log_info "')
    ]

    assert effective_mkdir == []


def test_install_script_initializes_env_file_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.env_file init" in content
    assert 'cp "$INSTALL_DIR/.env.example" "$ENV_FILE"' not in content


def test_install_script_backs_up_config_through_yaml_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.yaml_file backup" in content
    assert 'cp "$CONFIG_PATH" "$BACKUP"' not in content


def test_install_script_backs_up_sqlite_through_postgres_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_postgres" in content
    assert "--backup-sqlite" in content
    assert 'cp "$sqlite_db" "$backup"' not in content


def test_install_script_formats_sqlite_size_through_postgres_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--file-size" in content
    assert 'du -h "$sqlite_db"' not in content
    assert "cut -f1" not in content


def test_install_script_detects_existing_torch_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_torch --installed-cuda" in content
    assert 'python -c "import torch"' not in content


def test_install_script_checks_models_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_models cohere-ok" in content
    assert "-m transcria.install_models pyannote-cache" in content
    assert "-m transcria.install_models first-gguf" in content
    assert "-m transcria.install_models download-pyannote" in content
    assert "from pathlib import Path; import sys" not in content
    assert "Pipeline.from_pretrained('pyannote/speaker-diarization-community-1'" not in content
    assert 'find "$HF_CACHE"' not in content
    assert 'find "$INSTALL_DIR/models"' not in content


def test_install_script_checks_runtime_binaries_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_prerequisites" in content
    assert "--required ffmpeg" in content
    assert "--required ffprobe" in content
    assert "--optional lsof" in content
    assert 'for bin in ffmpeg ffprobe' not in content
    assert 'command -v lsof' not in content


def test_install_script_finds_download_clients_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "first-available --name hf --name huggingface-cli" in content
    assert "first-available --name huggingface-cli" in content
    assert "first-available --name llama-server" in content
    assert "check-binaries --required psql" in content
    assert "command -v psql" not in content
    assert "command -v huggingface-cli" not in content
    assert "command -v hf" not in content
    assert "command -v llama-server" not in content


def test_install_script_detects_system_capabilities_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "system-capabilities --format shell" in content
    assert "HAVE_SUDO" in content
    assert "HAVE_SYSTEMCTL" in content
    assert "command -v sudo" not in content
    assert "command -v runuser" not in content
    assert "command -v systemctl" not in content
    assert "command -v service" not in content
    assert "command -v nvidia-smi" not in content


def test_install_script_reads_opencode_version_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_opencode" in content
    assert "head -1" not in content


def test_install_script_finds_opencode_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_opencode" in content
    assert "--find" in content
    assert "command -v opencode" not in content
    assert "OPENCODE_BIN=$(which opencode)" not in content


def test_install_script_updates_opencode_path_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--ensure-path" in content
    assert 'echo "$PATH" | grep -q "$OPENCODE_DIR"' not in content
    assert 'echo "export PATH="$OPENCODE_DIR' not in content


def test_install_script_generates_postgres_password_through_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--generate-password" in content
    assert 'python -c "import secrets; print(secrets.token_urlsafe(24))"' not in content


def test_install_script_validates_postgres_inputs_through_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "--validate-inputs" in content
    assert '[[ ! "$PG_DB" =~' not in content
    assert '[[ ! "$PG_USER" =~' not in content
    assert '[[ ! "$PG_PORT" =~' not in content


def test_install_script_does_not_prescan_pg_hba_with_shell_grep():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "pg_hba_result=$(pg_admin_python_module transcria.install_postgres" in content
    assert "grep -qE '^host[[:space:]]+(all|replication)" not in content


def test_install_script_counts_change_me_through_yaml_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.yaml_file count-text" in content
    assert "grep -c 'CHANGE-ME'" not in content


def test_install_script_checks_existing_proxy_through_env_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.config.env_file has-any" in content
    assert "grep -qE '^https?_proxy='" not in content


def test_install_plan_matches_python_profile_matrix_for_main_profiles():
    cases = [
        ("all-in-one", []),
        ("web", []),
        ("scheduler", []),
        ("resource-node", []),
        ("migrate", []),
        ("all-in-one", ["--no-service"]),
    ]

    for profile, extra_args in cases:
        result = _run_install("--profile", profile, *extra_args, "--plan")
        assert result.returncode == 0, result.stdout + result.stderr
        observed = _parse_plan(result.stdout)
        expected = resolve_install_plan(profile, systemd="--no-service" not in extra_args)

        assert observed["profile"] == expected.profile
        assert observed["legacy_service"] == expected.legacy_service
        assert observed["inference_service"] == expected.inference_service
        assert observed["setup_postgres"] == ("prompt" if expected.setup_postgres is None else expected.setup_postgres)
        assert observed["needs_local_models"] == expected.needs_local_models
        assert observed["needs_llm"] == expected.needs_llm
        assert observed["needs_admin_config"] == expected.needs_admin_config
        assert tuple(observed["systemd_units"]) == expected.systemd_units


def test_plan_web_is_side_effect_free_contract():
    result = _run_install("--profile", "web", "--plan")

    assert result.returncode == 0
    assert "TranscrIA install plan" in result.stdout
    assert "profile=web" in result.stdout
    assert "setup_postgres=true" in result.stdout
    assert "needs_local_models=false" in result.stdout
    assert "needs_llm=false" in result.stdout
    assert "doctor_enabled=true" in result.stdout
    assert "transcria-migrate.service" in result.stdout
    assert "transcria-web.service" in result.stdout
    assert "Vérification des prérequis" not in result.stdout


def test_invalid_profile_fails_before_plan():
    result = _run_install("--profile", "bad", "--plan")

    assert result.returncode == 1
    assert "profil inconnu" in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_plan_resource_node_defaults_to_no_postgres_and_no_llm():
    result = _run_install("--profile", "resource-node", "--dry-run")

    assert result.returncode == 0
    assert "profile=resource-node" in result.stdout
    assert "runtime_role=none" in result.stdout
    assert "inference_service=true" in result.stdout
    assert "setup_postgres=false" in result.stdout
    assert "needs_local_models=true" in result.stdout
    assert "needs_llm=false" in result.stdout
    assert "needs_admin_config=false" in result.stdout
    assert "transcria-inference.service" in result.stdout


def test_plan_all_in_one_honors_no_service_and_no_torch():
    result = _run_install("--profile", "all-in-one", "--no-service", "--no-torch", "--plan")

    assert result.returncode == 0
    assert "profile=all-in-one" in result.stdout
    assert "runtime_role=all" in result.stdout
    assert "systemd=false" in result.stdout
    assert "legacy_service=false" in result.stdout
    assert "install_torch=false" in result.stdout
    assert "needs_llm=true" in result.stdout
    assert "  - none" in result.stdout


def test_plan_honors_skip_doctor():
    result = _run_install("--profile", "web", "--skip-doctor", "--plan")

    assert result.returncode == 0
    assert "doctor_profile=web" in result.stdout
    assert "doctor_enabled=false" in result.stdout
    assert "doctor_strict=false" in result.stdout
    assert "needs_llm=false" in result.stdout
    assert "  - transcria-web.service" in result.stdout


def test_plan_honors_strict_doctor():
    result = _run_install("--profile", "web", "--strict-doctor", "--plan")

    assert result.returncode == 0
    assert "doctor_enabled=true" in result.stdout
    assert "doctor_strict=true" in result.stdout


def test_plan_rejects_skip_and_strict_doctor():
    result = _run_install("--profile", "web", "--skip-doctor", "--strict-doctor", "--plan")

    assert result.returncode == 1
    assert "--skip-doctor et --strict-doctor sont incompatibles" in result.stdout


def test_invalid_split_sqlite_combination_fails_before_plan():
    result = _run_install("--profile", "scheduler", "--no-postgres", "--plan")

    assert result.returncode == 1
    assert "--profile scheduler nécessite PostgreSQL ; SQLite dev est incompatible." in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_sqlite_dev_alias_is_explicit_for_all_in_one_plan():
    result = _run_install("--profile", "all-in-one", "--sqlite-dev", "--plan")

    assert result.returncode == 0
    assert "profile=all-in-one" in result.stdout
    assert "setup_postgres=false" in result.stdout


def test_sqlite_dev_alias_is_rejected_for_split_profile():
    result = _run_install("--profile", "web", "--allow-sqlite-dev", "--plan")

    assert result.returncode == 1
    assert "--profile web nécessite PostgreSQL ; SQLite dev est incompatible." in result.stdout
    assert "TranscrIA install plan" not in result.stdout


def test_inference_alias_is_resource_node_plan():
    result = _run_install("--inference-service", "--plan")

    assert result.returncode == 0
    assert "profile=resource-node" in result.stdout
    assert "inference_service=true" in result.stdout


def test_inference_alias_conflicts_with_web_profile():
    result = _run_install("--profile", "web", "--inference-service", "--plan")

    assert result.returncode == 1
    assert "--inference-service est incompatible avec --profile web" in result.stdout
