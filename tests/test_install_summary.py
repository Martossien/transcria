from __future__ import annotations

import pytest

from transcria.install_summary import (
    main,
    parse_non_negative_int,
    render_configuration_summary,
    render_database_summary,
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
