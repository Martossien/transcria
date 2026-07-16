"""Contrats sans effet de bord de `install.sh`.

Ces tests n'exécutent que `--plan` ou des validations d'arguments qui sortent avant
toute création de venv/config/.env/service. Ils verrouillent les décisions de profil
sans lancer l'installation réelle.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from transcria.installer.profiles import resolve_install_plan

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

    assert "transcria.installer.cli prerequisites user-home" in content
    assert "pwd.getpwnam" not in content
    assert "python3 -c" not in content


def test_install_script_delegates_prerequisite_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.installer.cli prerequisites setup-log" in content
    assert "log_prerequisite_event" in content
    assert "Python $version :" not in content
    assert "Python 3.11+ requis. Installer avec: apt install python3.11" in content
    assert "nvidia-smi — $GPU_COUNT" not in content
    assert "nvidia-smi non trouvé ou inutilisable" not in content
    assert "$name : $path" not in content
    assert "$name manquant. Installer avec: apt install ffmpeg" not in content
    assert "$name manquant." not in content
    assert "lsof manquant — requis par start.sh/stop.sh" not in content


def test_install_script_uses_run_indented_for_command_output_prefixing():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "run_indented()" in content
    assert "run_indented env MODELS_DIR=" in content
    assert "2>&1 | sed 's/^/  /'" not in content
    assert "print_indented_file" in content
    assert "sed 's/^/  /'" not in content


def test_install_script_delegates_systemd_phase_to_installer_cli():
    # SECTION 11 (plan, rendu, copie privilégiée + daemon-reload/enable, repli .adapted)
    # est orchestrée par l'installateur Python ; le shell ne fait plus que câbler.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli systemd" in content
    assert "--have-sudo" in content and "--have-systemctl" in content
    assert "--service-home" in content and "--venv-dir" in content
    # Plus aucune mécanique d'installation d'unité inline.
    assert "install_systemd_unit()" not in content
    assert 'sudo cp "$rendered" "$dst"' not in content
    assert 'cp "$rendered" "$dst"' not in content
    assert 'systemctl enable "$unit"' not in content


def test_install_script_delegates_local_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.installer.cli paths" in content
    assert "--setup-log" in content
    assert "log_local_setup_event" in content
    assert "Venv existant :" not in content
    assert "Création du venv..." not in content
    assert "Venv créé :" not in content
    assert "Mise à jour de pip..." not in content
    assert "Installation requirements.txt..." not in content
    assert "requirements.txt installé" not in content
    assert "jobs/, models/, instance/ prêts" not in content


def test_install_script_delegates_python_env_phase_to_installer_cli():
    # SECTION 2-4 (venv + PyTorch + dépendances) est orchestrée par l'installateur
    # Python (transcria.installer.cli) ; aucune mécanique PyTorch ne reste inline.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli" in content
    assert "python-env" in content
    assert "log_torch_event" not in content
    assert "pip install torch" not in content
    assert "download.pytorch.org/whl" not in content
    assert "PyTorch déjà installé" not in content


def test_install_script_has_no_inline_split_systemd_mechanics():
    content = _INSTALL.read_text(encoding="utf-8")

    # Le plan d'unités et le rendu split sont construits en process par la phase Python ;
    # plus de boucle `--unit-plan`/`--kind` ni de tampons d'unités dans le shell.
    assert "--unit-plan" not in content
    assert "--install-unit" not in content
    assert "install_deploy_unit()" not in content
    assert "render_deploy_unit()" not in content
    assert "UNIT_ADAPTED" not in content
    assert "TMP_MIGRATE=" not in content
    assert "TMP_WEB=" not in content
    assert "TMP_SCHEDULER=" not in content


def test_install_script_delegates_systemd_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    # Les messages systemd sont désormais rendus par la phase Python (in-process),
    # plus via `--setup-log` ni un helper shell.
    assert "-m transcria.install_systemd --setup-log" not in content
    assert "log_systemd_event" not in content
    assert "Service $unit non installé (--no-service)" not in content
    assert "Service $unit installé et activé" not in content
    assert "sudo indisponible — fichier adapté" not in content
    assert "Pour installer :" not in content
    assert "sudo cp $adapted $dst" not in content
    assert "sudo systemctl daemon-reload && sudo systemctl enable $unit" not in content
    assert "$unit.service introuvable — service non installé" not in content
    assert "transcria.service introuvable — service non installé" not in content
    assert "transcria.service est déjà activé" not in content
    assert "sudo systemctl disable --now transcria.service" not in content
    assert "transcria-inference.service introuvable" not in content
    assert "Vérifiez que deploy/transcria-inference.service existe" not in content


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


def test_install_script_filters_helper_outputs_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'eval "$HF_COHERE_OUT"' not in content
    assert 'eval "$LLAMA_FALLBACK_OUT"' not in content
    assert 'eval "$HF_DL_OUT"' not in content


def test_install_script_filters_named_helper_outputs_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "eval_named_shell_assignments" in content
    assert "HAVE_NVIDIA_SMI HAVE_RUNUSER HAVE_SERVICE HAVE_SUDO HAVE_SYSTEMCTL" in content
    assert "GPU_COUNT CUDA_VER_FROM_SMI NVIDIA_WARNING GPU_VRAM_TOTAL_MB GPU_VRAM_MAX_MB GPU_SIZES_CSV" in content
    # (le plan PyTorch n'est plus évalué en shell : orchestré par transcria.installer.cli)
    assert 'eval "$SYSTEM_CAPABILITIES_OUT"' not in content
    assert 'eval "$NVIDIA_DETECT_OUT"' not in content
    assert 'eval "$TORCH_TAG_OUT"' not in content


def test_install_script_filters_profile_plan_output_before_eval():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "INSTALL_PROFILE INSTALL_RUNTIME_ROLE INSTALL_SERVICE INSTALL_INFERENCE SETUP_PG" in content
    assert "PROFILE_NEEDS_LOCAL_MODELS PROFILE_NEEDS_LLM PROFILE_NEEDS_ADMIN_CONFIG" in content
    assert 'eval "$plan_shell"' not in content


def test_install_script_delegates_final_summary_to_installer_cli():
    # SECTION 12 (en-tête profil, modèles, base, config restante, démarrage) est rendue
    # en une invocation par l'installateur Python ; plus de wrappers print_* ni de
    # sous-processus de rendu enchaînés, et le décompte CHANGE-ME est fait en process.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli summary" in content
    assert "--db-backend" in content and "--doctor-status" in content
    assert "print_profile_text" not in content
    assert "print_model_summary" not in content
    assert "print_database_summary" not in content
    assert "print_configuration_summary" not in content
    assert "-m transcria.install_models summary" not in content
    assert "python_module transcria.install_summary database" not in content
    assert "python_module transcria.install_summary configuration" not in content
    assert 'echo -e "${BOLD}Lancer le nœud de ressources' not in content
    assert 'echo -e "${BOLD}Modèles IA' not in content


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


def test_install_script_delegates_opencode_phase_to_installer_cli():
    # SECTION 9 (détection / installation / configuration opencode) est orchestrée par
    # l'installateur Python ; le shell ne fait plus aucune mécanique opencode inline.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli opencode" in content
    assert "--opencode-home" in content
    assert "--needs-llm" in content
    # Plus aucune mécanique opencode inline (détection, install, PATH, messages) en shell.
    assert "opencode_helper" not in content
    assert "log_opencode_setup_event" not in content
    assert "--detect" not in content
    assert "--ensure-path" not in content
    assert "command -v opencode" not in content
    assert "OPENCODE_BIN=$(which opencode)" not in content
    assert 'curl -fsSL -o "$OPENCODE_DEST"' not in content
    assert 'chmod +x "$OPENCODE_DEST"' not in content
    assert 'chown -R "$SERVICE_USER:" "$OPENCODE_HOME/.opencode"' not in content
    assert "Configuration du provider opencode local" not in content
    assert "Installation manuelle :" not in content


def test_install_script_recovers_opencode_bin_from_config_after_phase():
    # Régression (finding 8.4) : la phase opencode est un SOUS-PROCESSUS → elle ne peut pas
    # réassigner la variable shell OPENCODE_BIN. install.sh doit la RÉCUPÉRER depuis config.yaml
    # APRÈS la délégation, sinon SECTION 9-bis (sélection LLM d'arbitrage) se croit « opencode
    # manquant » et se saute silencieusement même opencode installé.
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'OPENCODE_BIN=$(yaml_get "workflow.arbitration_llm.opencode_bin")' in content
    recover = content.index('OPENCODE_BIN=$(yaml_get')
    delegate = content.index("transcria.installer.cli opencode")
    usage = content.index('[[ -z "${OPENCODE_BIN:-}" ]]')
    assert delegate < recover < usage, "OPENCODE_BIN doit être récupéré APRÈS la phase opencode et AVANT la sélection LLM"


def test_install_script_delegates_llm_selection_setup_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_arbitrage --setup-log" in content
    assert "nvidia-smi --query-gpu=memory.total" not in content
    assert "scripts/plan_llm_placement.py" not in content
    assert "--placement-plan" in content
    assert "--apply-placement-calibration" in content
    assert "load_llm_tier_metadata" in content
    assert "--tier-info" in content
    assert "log_llm_setup_event" in content
    assert "declare -A LLM_REPO" not in content
    assert "recommend_llm_tier()" not in content
    assert "Qwen3.6-35B-A3B-UD-IQ4_NL_XL" not in content
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


def test_install_script_delegates_llm_download_and_activation_logs():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "log_llm_setup_event llama-qualified" in content
    assert "log_llm_setup_event model-downloaded" in content
    assert "llama-server qualifié :" not in content
    assert "llama-server trouvé mais NON utilisable" not in content
    assert "Libs llama hors chemins standard" not in content
    assert "Modèle déjà présent :" not in content
    assert "Ni 'hf' ni 'huggingface-cli' trouvés" not in content
    assert "peut prendre plusieurs minutes" not in content
    assert "Modèle téléchargé :" not in content
    assert "Téléchargement échoué — vérifiez la connectivité / le HF_TOKEN" not in content
    assert "Téléchargement ignoré." not in content
    assert "alias générique 'arbitrage'" not in content
    assert "Calibration GPU écrite" not in content
    assert "Calibration auto échouée" not in content
    assert "Démarrage de la LLM" not in content
    assert "Bascule de palier incomplète" not in content
    assert "Modèle absent — palier non activé" not in content
    assert "LLM d'arbitrage ignoré" not in content
    assert "après téléchargement du modèle" not in content


def test_install_script_delegates_llm_prompts():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "arbitrage_helper --prompt tier" in content
    assert "arbitrage_helper --prompt models-dir" in content
    assert "arbitrage_helper --prompt llama-server" in content
    assert "--prompt download" in content
    assert "Palier LLM à installer" not in content
    assert "Répertoire de téléchargement des modèles" not in content
    assert "Chemin du binaire llama-server" not in content
    assert "Télécharger ${LLM_LABEL[$LLM_TIER]} depuis $REPO" not in content


def test_install_script_has_no_inline_final_status_rendering():
    content = _INSTALL.read_text(encoding="utf-8")

    # Base + configuration du résumé sont rendues par la phase summary (in-process).
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
    assert "doctor.py sauté à la demande" not in content
    assert "doctor.py : aucun échec bloquant" not in content
    assert "doctor.py a détecté des points à corriger" not in content
    assert "doctor.py non disponible — validation post-install sautée" not in content


def test_install_script_delegates_postgres_phase_to_installer_cli():
    # SECTION 6.5 — chemin post-connexion (connexion, encodage, DSN, état, Alembic,
    # migration SQLite) fondu dans l'installateur Python. Le shell ne garde que le
    # bootstrap local privilégié (pg_hba/rôle/base) puis délègue le reste.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli postgres" in content
    # Câblage attendu vers la phase Python.
    assert "--venv-python" in content
    assert "--admin-psql" in content  # identité privilégiée pour le rebuild local
    assert '--local-pg' in content and '--pg-migrate' in content
    # La mécanique de décision/exécution n'est plus dans le shell.
    assert 'case "$schema_action" in' not in content
    assert 'case "$sqlite_migration_action" in' not in content
    assert '"$VENV/bin/alembic" upgrade head' not in content
    assert "_do_pg_migrate" not in content
    assert "pg_app_psql" not in content


def test_install_script_no_longer_renders_postgres_tail_logs_inline():
    # Les sous-commandes de rendu du chemin post-connexion sont appelées en process
    # par la phase Python, plus jamais depuis le shell.
    content = _INSTALL.read_text(encoding="utf-8")

    for flag in (
        "--schema-action",
        "--schema-action-log",
        "--alembic-log",
        "--sqlite-migration-action",
        "--sqlite-migration-log",
        "--sqlite-migration-prompt",
        "--run-sqlite-migration",
        "--state-summary",
        "--connection-failure",
        "--encoding-warnings",
        "--file-size",
    ):
        assert flag not in content, flag
    for func in ("log_postgres_alembic_event", "log_sqlite_migration_event", "log_postgres_schema_action_event"):
        assert func not in content, func


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


def test_install_script_delegates_postgres_bootstrap_to_installer_cli():
    # Le bootstrap local privilégié (pg_hba + rôle + base) est orchestré par la phase
    # Python ; le shell ne fait que câbler l'identité postgres privilégiée.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli postgres-bootstrap" in content
    assert "--admin-psql" in content and "--admin-python" in content
    # Plus aucune mécanique SQL/psql/pg_hba inline pour le bootstrap.
    assert "--role-sql" not in content
    assert "--database-sql" not in content
    assert "--fallback-locale-c" not in content
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


def test_install_script_has_no_inline_postgres_state_queries():
    content = _INSTALL.read_text(encoding="utf-8")

    # Toutes les lectures d'état (existence base, comptes, encodage, version Alembic)
    # sont désormais en process dans les phases Python — plus de psql/SQL inline.
    assert "pg_state_query" not in content
    assert "--state-query" not in content
    assert "SELECT COUNT(*) FROM users" not in content
    assert "SELECT version_num FROM alembic_version" not in content
    assert "pg_encoding_to_char" not in content
    assert "tables public=$has_schema" not in content


def test_install_script_has_no_inline_pg_hba_rewrite_parsing():
    content = _INSTALL.read_text(encoding="utf-8")

    # La réécriture pg_hba + l'interprétation du résultat sont dans la phase bootstrap Python.
    assert "--pg-hba-rewrite-result" not in content
    assert '"$pg_hba_result" =~ ^changed=' not in content
    assert '[[ "$pg_hba_result" != "changed=0" ]]' not in content


def test_install_script_delegates_postgres_encoding_warnings():
    # Garde d'encodage UTF8 désormais portée par la phase Python (in-process).
    content = _INSTALL.read_text(encoding="utf-8")

    assert "texte stocké SANS validation d'encodage" not in content
    assert "L'application force client_encoding=utf8" not in content


def test_install_script_delegates_postgres_connection_failure_messages():
    # Test de connexion + message d'échec portés par la phase Python.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "Connexion PostgreSQL impossible avec le rôle" not in content
    assert "Créez la base et le rôle côté serveur" not in content


def test_install_script_uses_requirements_as_runtime_dependency_source():
    content = _INSTALL.read_text(encoding="utf-8")

    # requirements.txt reste la source unique des dépendances runtime, désormais
    # passée à l'installateur Python plutôt qu'à un pip inline.
    assert '--requirements "$INSTALL_DIR/requirements.txt"' in content
    assert 'pip install -r "$INSTALL_DIR/requirements.txt" --quiet' not in content
    assert "pip install accelerate" not in content
    assert "pip install python-dotenv" not in content


def test_install_script_delegates_common_runtime_directories_to_python():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.installer.cli paths" in content
    assert 'mkdir -p "$INSTALL_DIR/jobs" "$INSTALL_DIR/models/cohere-asr" "$INSTALL_DIR/instance"' not in content


def test_install_script_has_no_effective_mkdir_calls():
    content = _INSTALL.read_text(encoding="utf-8")

    effective_mkdir = [
        line.strip()
        for line in content.splitlines()
        if line.strip().startswith("mkdir -p") and not line.strip().startswith('log_info "')
    ]

    assert effective_mkdir == []


def test_install_script_delegates_config_phase_to_installer_cli():
    # SECTION 6 (cœur déterministe : config.yaml, sauvegarde, .env, secrets, rôle)
    # est orchestrée par l'installateur Python ; le shell ne fait plus l'init/backup.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli config" in content
    assert "--example-config" in content and "--env-template" in content
    # Plus aucune mécanique de génération/backup inline dans le shell.
    assert "-m transcria.config.env_file init" not in content
    assert "-m transcria.config.yaml_file backup" not in content
    assert 'cp "$INSTALL_DIR/.env.example" "$ENV_FILE"' not in content
    assert 'cp "$CONFIG_PATH" "$BACKUP"' not in content


def test_install_script_backs_up_sqlite_through_python_phase():
    # La sauvegarde + migration SQLite→PG est exécutée par la phase Python
    # (run_sqlite_migration), plus par un cp inline ni un helper appelé du shell.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.install_postgres" in content  # bootstrap local (rôle/base/pg_hba)
    assert 'cp "$sqlite_db" "$backup"' not in content


def test_install_script_formats_sqlite_size_through_python_phase():
    # Taille SQLite formatée en process (human_file_size) par la phase, plus par du/cut.
    content = _INSTALL.read_text(encoding="utf-8")

    assert 'du -h "$sqlite_db"' not in content
    assert "cut -f1" not in content


def test_install_script_delegates_torch_detection_to_installer():
    content = _INSTALL.read_text(encoding="utf-8")

    # La détection et l'installation de PyTorch ont quitté le shell pour
    # l'installateur Python (transcria.installer.python_env, via install_torch).
    assert "transcria.install_torch" not in content
    assert "--install-plan" not in content
    assert "--installed-cuda" not in content
    assert 'python -c "import torch"' not in content


def test_install_script_checks_models_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "python_module transcria.install_models detect-local" in content
    assert "eval_named_shell_assignments \"$MODEL_DETECTION\"" in content
    assert "-m transcria.install_models cohere-ok" not in content
    assert "-m transcria.install_models pyannote-cache" not in content
    assert "-m transcria.install_models first-gguf" not in content
    assert "-m transcria.install_models download-pyannote" in content
    assert "from pathlib import Path; import sys" not in content
    assert "Pipeline.from_pretrained('pyannote/speaker-diarization-community-1'" not in content
    assert 'find "$HF_CACHE"' not in content
    assert 'find "$INSTALL_DIR/models"' not in content


def test_install_script_checks_runtime_binaries_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "-m transcria.installer.cli prerequisites" in content
    assert "--required ffmpeg" in content
    assert "--required ffprobe" in content
    assert "--optional lsof" in content
    assert 'for bin in ffmpeg ffprobe' not in content
    assert 'command -v lsof' not in content


def test_install_script_finds_download_clients_through_python_helper():
    content = _INSTALL.read_text(encoding="utf-8")

    assert "cohere-download-plan" in content
    assert "arbitrage_helper --download-client" in content
    assert "first-available --name hf --name huggingface-cli" not in content
    assert "first-available --name huggingface-cli --format shell" not in content
    assert "arbitrage_helper --llama-detect" in content
    assert "arbitrage_helper --llama-fallback" in content
    assert "first-available --name llama-server" not in content
    assert "scripts/detect_llama_server.py" not in content
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

    # pg_hba est lu/réécrit par la phase bootstrap Python (via l'identité postgres),
    # jamais pré-scanné par un grep shell.
    assert "pg_admin_python_module" not in content
    assert "grep -qE '^host[[:space:]]+(all|replication)" not in content


def test_install_script_counts_change_me_in_summary_phase_not_shell():
    content = _INSTALL.read_text(encoding="utf-8")

    # Le décompte des CHANGE-ME résiduels est fait en process par la phase summary,
    # plus via un helper appelé du shell ni un grep.
    assert "-m transcria.config.yaml_file count-text" not in content
    assert "grep -c 'CHANGE-ME'" not in content


def test_install_script_delegates_proxy_persistence_to_installer_cli():
    # Le gate lit l'environnement du shell installateur (resté en shell) ; la décision
    # (déjà-présent / confirmation / persistance + chown) est déléguée à la phase Python.
    content = _INSTALL.read_text(encoding="utf-8")

    assert "transcria.installer.cli config-proxy" in content
    assert "--proxy-https" in content and "--proxy-no" in content
    # Plus de mécanique inline : ni has-any, ni grep, ni env_set/ask_yn pour le proxy.
    assert "-m transcria.config.env_file has-any" not in content
    assert "grep -qE '^https?_proxy='" not in content
    assert "PERSIST_PROXY" not in content


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


def test_plan_accepts_skip_deps_and_implies_no_torch():
    # --skip-deps suppose un environnement Python déjà provisionné (couche build
    # Docker, venv existant) ; comme torch est une dépendance, il implique --no-torch.
    result = _run_install("--profile", "web", "--skip-deps", "--plan")

    assert result.returncode == 0
    assert "profile=web" in result.stdout
    assert "install_torch=false" in result.stdout


def test_plan_accepts_pg_existing():
    # --pg-existing (base déjà provisionnée : Docker / base distante / migrate) est
    # accepté sans erreur et n'altère pas le rendu du plan.
    result = _run_install("--profile", "web", "--pg-existing", "--plan")

    assert result.returncode == 0
    assert "profile=web" in result.stdout
