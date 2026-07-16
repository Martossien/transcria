from __future__ import annotations

import pytest

from transcria.installer.summary_lib import (
    main,
    parse_non_negative_int,
    render_configuration_summary,
    render_database_summary,
    render_setup_log,
)


def test_parse_non_negative_int_accepts_zero_and_positive():
    assert parse_non_negative_int("0") == 0
    assert parse_non_negative_int("12") == 12


def test_parse_non_negative_int_rejects_invalid_values():
    with pytest.raises(ValueError, match="entier invalide"):
        parse_non_negative_int("abc")
    with pytest.raises(ValueError, match="entier négatif"):
        parse_non_negative_int("-1")


def test_render_database_summary_for_postgresql():
    assert render_database_summary("PostgreSQL (transcria@127.0.0.1:5432)") == (
        "Base de données :\n"
        "  [OK] PostgreSQL (transcria@127.0.0.1:5432) — DSN dans .env (TRANSCRIA_DATABASE_URL)\n"
    )


def test_render_database_summary_for_sqlite_dev():
    assert render_database_summary("SQLite") == (
        "Base de données :\n"
        "  [INFO] SQLite — réservé au dev local ; passez à PostgreSQL hors dev : ./install.sh --postgres\n"
    )


def test_render_configuration_summary_with_remaining_placeholders():
    assert render_configuration_summary(config_path="/opt/transcria/config.yaml", remaining_changes=2, doctor_status="WARN/FAIL") == (
        "Configuration :\n"
        "  [WARN] /opt/transcria/config.yaml contient encore 2 valeur(s) 'CHANGE-ME'\n"
        "         Éditer config.yaml avant le premier démarrage\n"
        "  [INFO] doctor.py : WARN/FAIL\n"
    )


def test_render_configuration_summary_without_remaining_placeholders():
    assert render_configuration_summary(config_path="/opt/transcria/config.yaml", remaining_changes=0, doctor_status="OK") == (
        "Configuration :\n"
        "  [OK] config.yaml — aucune valeur par défaut restante\n"
        "  [INFO] doctor.py : OK\n"
    )


def test_install_summary_cli_prints_database(capsys):
    assert main(["database", "--db-backend", "SQLite"]) == 0

    assert "[INFO] SQLite" in capsys.readouterr().out


def test_install_summary_cli_prints_configuration(capsys):
    assert main([
        "configuration",
        "--config-path", "/opt/transcria/config.yaml",
        "--remaining-changes", "0",
        "--doctor-status", "OK",
    ]) == 0

    assert "doctor.py : OK" in capsys.readouterr().out


def test_install_summary_cli_rejects_invalid_remaining_changes(capsys):
    assert main([
        "configuration",
        "--config-path", "/opt/transcria/config.yaml",
        "--remaining-changes", "-2",
        "--doctor-status", "OK",
    ]) == 2

    assert "entier négatif invalide" in capsys.readouterr().err


def test_render_setup_log_for_config_file_events():
    assert render_setup_log(event="config-kept") == "OK:config.yaml existant conservé\n"
    assert render_setup_log(event="force-hint") == "INFO:(--force-config pour régénérer)\n"
    assert render_setup_log(event="config-backup", value="/opt/transcria/config.yaml.20260101.bak") == (
        "INFO:Ancien config.yaml sauvegardé : /opt/transcria/config.yaml.20260101.bak\n"
    )
    assert render_setup_log(event="config-generate-start") == "INFO:Génération via bootstrap_config.py (auto-détection)...\n"
    assert render_setup_log(event="config-generated") == "OK:config.yaml généré\n"


def test_render_setup_log_for_env_and_profile_events():
    assert render_setup_log(event="secret-created") == "OK:Clé secrète Flask générée dans .env\n"
    assert render_setup_log(event="secret-present") == "OK:TRANSCRIA_SECRET présent dans .env\n"
    assert render_setup_log(event="profile-runtime", profile="web", runtime_role="web") == (
        "OK:Profil d'installation : web (TRANSCRIA_ROLE=web)\n"
    )
    assert render_setup_log(event="profile-all-default") == "OK:Profil d'installation : all-in-one (défaut)\n"
    assert render_setup_log(event="profile-resource-node") == "OK:Profil d'installation : resource-node (inference_service)\n"
    assert render_setup_log(event="profile-migrate") == "OK:Profil d'installation : migrate (Alembic only)\n"
    assert render_setup_log(event="profile-generic", profile="scheduler") == "OK:Profil d'installation : scheduler\n"
    assert render_setup_log(event="inference-key-present") == "OK:TRANSCRIA_INFERENCE_API_KEY présent dans .env\n"
    assert render_setup_log(event="inference-key-created") == "OK:TRANSCRIA_INFERENCE_API_KEY généré dans .env (chmod 600)\n"
    assert render_setup_log(event="proxy-present") == "OK:Proxy déjà présent dans .env\n"
    assert render_setup_log(event="proxy-persisted") == "OK:Proxy persisté dans .env (http_proxy/https_proxy/no_proxy)\n"
    assert render_setup_log(event="admin-default-password") == "WARN:Mot de passe admin : valeur par défaut 'CHANGE-ME'\n"
    assert render_setup_log(event="admin-password-set") == "OK:Mot de passe admin défini\n"
    assert render_setup_log(event="admin-password-too-short") == "WARN:Trop court — inchangé. Éditez config.yaml manuellement.\n"
    assert render_setup_log(event="config-updated") == "OK:config.yaml mis à jour\n"
    assert render_setup_log(event="env-secured", value="transcria") == "OK:.env sécurisé pour l'utilisateur de service (transcria)\n"


def test_render_setup_log_for_doctor_events():
    assert render_setup_log(event="doctor-skipped") == "WARN:doctor.py sauté à la demande (--skip-doctor)\n"
    assert render_setup_log(event="doctor-ok") == "OK:doctor.py : aucun échec bloquant\n"
    assert render_setup_log(event="doctor-warn") == "WARN:doctor.py a détecté des points à corriger avant production\n"
    assert render_setup_log(event="doctor-unavailable") == "WARN:doctor.py non disponible — validation post-install sautée\n"


def test_render_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement de configuration inconnu : bad"):
        render_setup_log(event="bad")


def test_install_summary_cli_prints_setup_log(capsys):
    assert main(["setup-log", "--event", "profile-runtime", "--profile", "web", "--runtime-role", "web"]) == 0

    assert capsys.readouterr().out == "OK:Profil d'installation : web (TRANSCRIA_ROLE=web)\n"
