from __future__ import annotations

import os
import stat
from types import SimpleNamespace

import pytest

from transcria.install_postgres import (
    backup_sqlite_database,
    build_pg_dsn,
    decide_schema_action,
    decide_sqlite_migration_action,
    generate_pg_password,
    human_file_size,
    is_local_pg_host,
    main,
    parse_bool,
    parse_non_negative_int,
    render_alembic_log,
    render_connection_failure,
    render_database_setup_log,
    render_database_sql,
    render_encoding_warnings,
    render_pg_hba_rewrite_result,
    render_role_sql,
    render_schema_action_log,
    render_setup_log,
    render_sqlite_migration_log,
    render_sqlite_migration_prompt,
    render_state_query,
    render_state_summary,
    rewrite_pg_hba_file,
    rewrite_pg_hba_for_tcp_password,
    run_sqlite_migration,
    validate_pg_inputs,
)


def test_is_local_pg_host_accepts_only_loopback_aliases():
    assert is_local_pg_host("127.0.0.1")
    assert is_local_pg_host("localhost")
    assert is_local_pg_host("::1")
    assert not is_local_pg_host("postgres.internal")
    assert not is_local_pg_host("10.0.0.5")


def test_build_pg_dsn_quotes_credentials_and_database_and_brackets_ipv6():
    dsn = build_pg_dsn("::1", "5433", "transcria prod", "user/name", "p@ss/word#1")

    assert dsn == "postgresql+psycopg://user%2Fname:p%40ss%2Fword%231@[::1]:5433/transcria%20prod"


def test_install_postgres_cli_outputs_dsn(capsys):
    result = main([
        "--dsn",
        "--host", "127.0.0.1",
        "--port", "5432",
        "--db", "transcria",
        "--user", "transcria",
        "--password", "secret!",
    ])

    assert result == 0
    assert capsys.readouterr().out.strip() == "postgresql+psycopg://transcria:secret%21@127.0.0.1:5432/transcria"


def test_install_postgres_cli_tests_local_host():
    assert main(["--is-local-host", "--host", "localhost"]) == 0
    assert main(["--is-local-host", "--host", "postgres.internal"]) == 1


def test_backup_sqlite_database_copies_to_backup_dir_and_preserves_mode(tmp_path):
    sqlite_db = tmp_path / "transcrIA.db"
    sqlite_db.write_bytes(b"sqlite-content")
    sqlite_db.chmod(0o640)
    backup_dir = tmp_path / "backups"

    backup = backup_sqlite_database(sqlite_db, backup_dir, "20260102_030405")

    assert backup == backup_dir / "transcrIA_20260102_030405.db.bak"
    assert backup.read_bytes() == b"sqlite-content"
    assert stat.S_IMODE(os.stat(backup).st_mode) == 0o640


def test_install_postgres_cli_backs_up_sqlite_database(tmp_path, capsys):
    sqlite_db = tmp_path / "transcrIA.db"
    sqlite_db.write_bytes(b"sqlite-content")
    backup_dir = tmp_path / "backups"

    result = main([
        "--backup-sqlite",
        "--sqlite-db", str(sqlite_db),
        "--backup-dir", str(backup_dir),
        "--suffix", "stamp",
    ])

    backup = backup_dir / "transcrIA_stamp.db.bak"
    assert result == 0
    assert capsys.readouterr().out == f"{backup}\n"
    assert backup.read_bytes() == b"sqlite-content"


def test_run_sqlite_migration_backs_up_and_runs_script(tmp_path, capsys, monkeypatch):
    sqlite_db = tmp_path / "transcrIA.db"
    sqlite_db.write_bytes(b"sqlite-content")
    backup_dir = tmp_path / "backups"
    install_dir = tmp_path / "repo"
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd: list[str], **kwargs):
        calls.append((cmd, kwargs["env"]))
        return SimpleNamespace(returncode=0, stdout="migrated\n", stderr="")

    monkeypatch.setattr("transcria.install_postgres.subprocess.run", fake_run)

    assert run_sqlite_migration(
        dsn="postgresql+psycopg://u:p@h/db",
        sqlite_db=sqlite_db,
        backup_dir=backup_dir,
        suffix="stamp",
        install_dir=install_dir,
        python_bin="/venv/bin/python",
    ) == 0

    out = capsys.readouterr().out
    assert "OK:Backup SQLite sauvegardé" in out
    assert "INFO:Migration des données SQLite" in out
    assert "INFO:  migrated" in out
    assert "OK:Données migrées" in out
    assert (backup_dir / "transcrIA_stamp.db.bak").read_bytes() == b"sqlite-content"
    assert calls[0][0] == [
        "/venv/bin/python",
        str(install_dir / "scripts" / "migrate_sqlite_to_postgres.py"),
        "--source",
        f"sqlite:///{sqlite_db}",
    ]
    assert calls[0][1]["TRANSCRIA_DATABASE_URL"] == "postgresql+psycopg://u:p@h/db"


def test_run_sqlite_migration_reports_script_failure(tmp_path, capsys, monkeypatch):
    sqlite_db = tmp_path / "transcrIA.db"
    sqlite_db.write_bytes(b"sqlite-content")

    def fake_run(cmd: list[str], **kwargs):
        return SimpleNamespace(returncode=7, stdout="", stderr="migration failed\n")

    monkeypatch.setattr("transcria.install_postgres.subprocess.run", fake_run)

    result = run_sqlite_migration(
        dsn="postgresql+psycopg://u:p@h/db",
        sqlite_db=sqlite_db,
        backup_dir=tmp_path / "backups",
        suffix="stamp",
        install_dir=tmp_path / "repo",
        python_bin="/venv/bin/python",
    )

    out = capsys.readouterr().out
    assert result == 7
    assert "INFO:  migration failed" in out
    assert "ERROR:Échec de la migration SQLite" in out
    assert "WARN:La base PostgreSQL est peut-être partiellement remplie" in out


def test_human_file_size_formats_without_shell_du(tmp_path):
    tiny = tmp_path / "tiny.db"
    tiny.write_bytes(b"abc")
    medium = tmp_path / "medium.db"
    medium.write_bytes(b"x" * 1536)

    assert human_file_size(tiny) == "3B"
    assert human_file_size(medium) == "1.5K"


def test_install_postgres_cli_outputs_file_size(tmp_path, capsys):
    sqlite_db = tmp_path / "transcrIA.db"
    sqlite_db.write_bytes(b"x" * 2048)

    assert main(["--file-size", str(sqlite_db)]) == 0

    assert capsys.readouterr().out == "2.0K\n"


def test_generate_pg_password_returns_urlsafe_secret():
    password = generate_pg_password()

    assert len(password) >= 24
    assert "\n" not in password
    assert ":" not in password


def test_install_postgres_cli_generates_password(capsys):
    assert main(["--generate-password"]) == 0

    password = capsys.readouterr().out.strip()
    assert len(password) >= 24


def test_validate_pg_inputs_accepts_valid_values():
    assert validate_pg_inputs("transcria", "transcria_user", "5432") == []
    assert validate_pg_inputs("_db", "_user", 1) == []
    assert validate_pg_inputs("a" * 63, "u" * 63, 65535) == []


def test_validate_pg_inputs_rejects_invalid_identifier_and_port():
    errors = validate_pg_inputs("1bad", "bad-name", "70000")

    assert errors == [
        "Nom de base invalide : '1bad' (attendu : [a-zA-Z_][a-zA-Z0-9_]{0,62})",
        "Nom de rôle invalide : 'bad-name' (attendu : [a-zA-Z_][a-zA-Z0-9_]{0,62})",
        "Port invalide : '70000' (attendu : 1-65535)",
    ]


def test_validate_pg_inputs_rejects_leading_zero_port_to_avoid_ambiguity():
    assert validate_pg_inputs("transcria", "transcria", "05432") == ["Port invalide : '05432' (attendu : 1-65535)"]


def test_parse_non_negative_int_for_postgres_counts():
    assert parse_non_negative_int("0", name="has_schema") == 0
    assert parse_non_negative_int(12, name="has_data") == 12


def test_parse_non_negative_int_rejects_invalid_counts():
    with pytest.raises(ValueError, match="has_schema invalide : abc"):
        parse_non_negative_int("abc", name="has_schema")

    with pytest.raises(ValueError, match="has_data négatif invalide : -1"):
        parse_non_negative_int("-1", name="has_data")


def test_parse_bool_for_postgres_cli_flags():
    assert parse_bool("true", name="flag")
    assert parse_bool("1", name="flag")
    assert not parse_bool("false", name="flag")
    assert not parse_bool("0", name="flag")


def test_parse_bool_rejects_invalid_value():
    with pytest.raises(ValueError, match="flag booléen invalide : maybe"):
        parse_bool("maybe", name="flag")


def test_decide_schema_action_from_database_state():
    assert decide_schema_action("5", "2") == "keep"
    assert decide_schema_action("5", "0") == "upgrade-existing"
    assert decide_schema_action("0", "0") == "create"


def test_install_postgres_cli_decides_schema_action(capsys):
    assert main(["--schema-action", "--has-schema", "3", "--has-data", "0"]) == 0

    assert capsys.readouterr().out == "upgrade-existing\n"


def test_install_postgres_cli_rejects_invalid_schema_action_counts(capsys):
    assert main(["--schema-action", "--has-schema", "bad", "--has-data", "0"]) == 2

    assert "has_schema invalide : bad" in capsys.readouterr().err


def test_decide_sqlite_migration_action():
    assert decide_sqlite_migration_action(sqlite_present=False, has_data=0, non_interactive=True, pg_migrate=True) == "none"
    assert decide_sqlite_migration_action(sqlite_present=True, has_data=1, non_interactive=True, pg_migrate=True) == "none"
    assert decide_sqlite_migration_action(sqlite_present=True, has_data=0, non_interactive=False, pg_migrate=False) == "prompt"
    assert decide_sqlite_migration_action(sqlite_present=True, has_data=0, non_interactive=True, pg_migrate=True) == "migrate"
    assert decide_sqlite_migration_action(sqlite_present=True, has_data=0, non_interactive=True, pg_migrate=False) == "skip"


def test_render_role_sql_is_stable():
    assert render_role_sql() == (
        "SELECT format('CREATE ROLE %I LOGIN PASSWORD %L', :'role', :'pwd')\n"
        "WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = :'role') \\gexec\n"
        "SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role', :'pwd') \\gexec\n"
    )


def test_render_database_sql_is_stable():
    assert render_database_sql() == (
        "SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', :'dbname', :'role', 'UTF8') \\gexec\n"
    )
    assert render_database_sql(fallback_locale_c=True) == (
        "SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L LC_COLLATE %L LC_CTYPE %L TEMPLATE template0',\n"
        "              :'dbname', :'role', 'UTF8', 'C', 'C') \\gexec\n"
    )


def test_install_postgres_cli_renders_role_and_database_sql(capsys):
    assert main(["--role-sql"]) == 0
    assert "CREATE ROLE" in capsys.readouterr().out

    assert main(["--database-sql", "--fallback-locale-c"]) == 0
    assert "LC_COLLATE" in capsys.readouterr().out


def test_render_state_query_is_stable():
    assert render_state_query("database-exists") == "SELECT 1 FROM pg_database WHERE datname = :'dbname';\n"
    assert render_state_query("encoding") == "SELECT pg_encoding_to_char(encoding) FROM pg_database WHERE datname = current_database();\n"
    assert render_state_query("public-table-count") == "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public'\n"
    assert render_state_query("users-count") == "SELECT COUNT(*) FROM users\n"
    assert render_state_query("alembic-version") == "SELECT version_num FROM alembic_version\n"


def test_render_state_query_rejects_unknown_name():
    with pytest.raises(ValueError, match="requête inconnue: bad"):
        render_state_query("bad")


def test_install_postgres_cli_renders_state_query(capsys):
    assert main(["--state-query", "users-count"]) == 0

    assert capsys.readouterr().out == "SELECT COUNT(*) FROM users\n"


def test_render_encoding_warnings_for_non_utf8_database():
    assert render_encoding_warnings("transcria", "SQL_ASCII") == (
        "La base 'transcria' existe déjà en encodage SQL_ASCII (UTF8 attendu) :\n"
        "texte stocké SANS validation d'encodage — migrez-la dès que possible\n"
        "(procédure : docs/INSTALL.md, section « Encodage de la base »).\n"
        "L'application force client_encoding=utf8 et reste fonctionnelle en attendant.\n"
    )


def test_render_encoding_warnings_ignores_utf8_or_empty_encoding():
    assert render_encoding_warnings("transcria", "UTF8") == ""
    assert render_encoding_warnings("transcria", "") == ""


def test_install_postgres_cli_renders_encoding_warnings(capsys):
    assert main(["--encoding-warnings", "--db", "transcria", "--encoding", "LATIN1"]) == 0

    assert "LATIN1" in capsys.readouterr().out


def test_render_connection_failure_for_local_postgres():
    assert render_connection_failure(db="transcria", user="app", host="127.0.0.1", port="5432", local_pg=True) == (
        "ERROR:Connexion PostgreSQL impossible avec le rôle 'app' sur 'transcria@127.0.0.1:5432'.\n"
        "WARN:Vérifiez pg_hba.conf et le reload PostgreSQL ; l'authentification TCP doit accepter le mot de passe.\n"
    )


def test_render_connection_failure_for_remote_postgres():
    assert render_connection_failure(db="transcria", user="app", host="db.internal", port="5432", local_pg=False) == (
        "ERROR:Connexion PostgreSQL impossible avec le rôle 'app' sur 'transcria@db.internal:5432'.\n"
        "WARN:Créez la base et le rôle côté serveur, puis relancez avec --pg-host/--pg-user/--pg-password.\n"
    )


def test_install_postgres_cli_renders_connection_failure(capsys):
    assert main([
        "--connection-failure",
        "--db", "transcria",
        "--user", "app",
        "--host", "db.internal",
        "--port", "5432",
        "--local-pg", "false",
    ]) == 0

    rendered = capsys.readouterr().out
    assert rendered.startswith("ERROR:Connexion PostgreSQL impossible")
    assert "Créez la base" in rendered


def test_render_state_summary_normalizes_database_counts():
    assert render_state_summary(db="transcria", has_schema="12", has_data="3", alembic_version="abc123") == (
        "Base 'transcria' : tables public=12 | alembic='abc123' | utilisateurs=3\n"
    )


def test_install_postgres_cli_renders_state_summary(capsys):
    assert main([
        "--state-summary",
        "--db", "transcria",
        "--has-schema", "12",
        "--has-data", "3",
        "--alembic-version", "abc123",
    ]) == 0

    assert capsys.readouterr().out == "Base 'transcria' : tables public=12 | alembic='abc123' | utilisateurs=3\n"


def test_render_schema_action_log_for_known_actions():
    assert render_schema_action_log(db="transcria", action="keep") == "OK:La base 'transcria' existe déjà avec des données. Conservation.\n"
    assert render_schema_action_log(db="transcria", action="upgrade-existing") == (
        "INFO:La base 'transcria' a le schéma mais est vide. Application des migrations Alembic…\n"
    )
    assert render_schema_action_log(db="transcria", action="create") == "INFO:Création du schéma (alembic upgrade head)…\n"


def test_render_schema_action_log_rejects_unknown_action():
    with pytest.raises(ValueError, match="action Alembic PostgreSQL inconnue : bad"):
        render_schema_action_log(db="transcria", action="bad")


def test_install_postgres_cli_renders_schema_action_log(capsys):
    assert main(["--schema-action-log", "--db", "transcria", "--action", "upgrade-existing"]) == 0

    assert capsys.readouterr().out == "INFO:La base 'transcria' a le schéma mais est vide. Application des migrations Alembic…\n"


def test_install_postgres_cli_decides_sqlite_migration_action(capsys):
    assert main([
        "--sqlite-migration-action",
        "--sqlite-present", "true",
        "--has-data", "0",
        "--non-interactive", "true",
        "--pg-migrate", "false",
    ]) == 0

    assert capsys.readouterr().out == "skip\n"


def test_install_postgres_cli_rejects_invalid_sqlite_migration_flags(capsys):
    assert main([
        "--sqlite-migration-action",
        "--sqlite-present", "maybe",
        "--has-data", "0",
        "--non-interactive", "true",
        "--pg-migrate", "false",
    ]) == 2

    assert "sqlite_present booléen invalide : maybe" in capsys.readouterr().err


def test_install_postgres_cli_validates_inputs(capsys):
    assert main(["--validate-inputs", "--db", "transcria", "--user", "transcria", "--port", "5432"]) == 0
    assert capsys.readouterr().out == ""

    assert main(["--validate-inputs", "--db", "bad-name", "--user", "transcria", "--port", "bad"]) == 1
    out = capsys.readouterr().out
    assert "Nom de base invalide" in out
    assert "Port invalide" in out


def test_render_pg_hba_rewrite_result_decides_reload_action():
    assert render_pg_hba_rewrite_result("changed=0") == "ACTION:none\n"
    assert render_pg_hba_rewrite_result("changed=2") == "INFO:Mise à jour de pg_hba.conf (ident/peer → scram-sha-256)…\nACTION:reload\n"


def test_render_pg_hba_rewrite_result_rejects_invalid_result():
    with pytest.raises(ValueError, match="résultat pg_hba.conf invalide : modified=1"):
        render_pg_hba_rewrite_result("modified=1")


def test_install_postgres_cli_renders_pg_hba_rewrite_result(capsys):
    assert main(["--pg-hba-rewrite-result", "--result", "changed=1"]) == 0

    assert capsys.readouterr().out == "INFO:Mise à jour de pg_hba.conf (ident/peer → scram-sha-256)…\nACTION:reload\n"


def test_render_setup_log_for_local_and_remote_postgres_events():
    assert render_setup_log(event="local-check", db="transcria", user="app", host="127.0.0.1") == (
        "INFO:Vérification du rôle 'app' et de la base 'transcria'…\n"
    )
    assert render_setup_log(event="local-ready", db="transcria", user="app", host="127.0.0.1") == "OK:Rôle et base PostgreSQL prêts\n"
    assert render_setup_log(event="remote-detected", db="transcria", user="app", host="db.internal") == (
        "INFO:PostgreSQL distant détecté (db.internal) : rôle/base supposés déjà créés.\n"
    )
    assert render_setup_log(event="connection-ok", db="transcria", user="app", host="db.internal") == "OK:Connexion PostgreSQL validée\n"
    assert render_setup_log(event="dsn-written", db="transcria", user="app", host="db.internal") == (
        "OK:DSN PostgreSQL écrit dans .env (chmod 600)\n"
    )


def test_render_setup_log_for_postgres_bootstrap_errors():
    assert render_setup_log(event="role-error", db="transcria", user="app", host="127.0.0.1") == (
        "ERROR:Échec de la création du rôle PostgreSQL — vérifiez les droits sudo/runuser sur le compte postgres.\n"
    )
    assert render_setup_log(event="database-fallback", db="transcria", user="app", host="127.0.0.1") == (
        "WARN:CREATE DATABASE UTF8 refusé (locale du cluster incompatible ?) — repli LC_COLLATE/LC_CTYPE 'C'…\n"
    )
    assert render_setup_log(event="database-error", db="transcria", user="app", host="127.0.0.1") == (
        "ERROR:Échec de la création de la base PostgreSQL en UTF8 — vérifiez les droits sudo/runuser sur le compte postgres.\n"
    )


def test_render_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement PostgreSQL inconnu : bad"):
        render_setup_log(event="bad", db="transcria", user="app", host="127.0.0.1")


def test_install_postgres_cli_renders_setup_log(capsys):
    assert main(["--setup-log", "--event", "remote-detected", "--db", "transcria", "--user", "app", "--host", "db.internal"]) == 0

    assert capsys.readouterr().out == "INFO:PostgreSQL distant détecté (db.internal) : rôle/base supposés déjà créés.\n"


def test_render_alembic_log_for_success_and_failure_events():
    assert render_alembic_log(event="upgrade-ok") == "OK:Schéma à jour (Alembic)\n"
    assert render_alembic_log(event="rebuild-start") == "ERROR:Alembic a échoué. Tentative de reconstruction locale…\n"
    assert render_alembic_log(event="rebuild-ok") == "OK:Schéma reconstruit\n"
    assert render_alembic_log(event="rebuild-failed") == "ERROR:Alembic a échoué une seconde fois. Arrêt.\n"
    assert render_alembic_log(event="remote-upgrade-failed") == (
        "ERROR:Alembic a échoué sur PostgreSQL distant. Reconstruction automatique refusée.\n"
    )
    assert render_alembic_log(event="create-ok") == "OK:Schéma PostgreSQL créé\n"
    assert render_alembic_log(event="create-failed") == "ERROR:Échec d'alembic upgrade head\n"
    assert render_alembic_log(event="unknown-action", action="bad") == "ERROR:Action Alembic PostgreSQL inconnue : bad\n"


def test_render_alembic_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement Alembic PostgreSQL inconnu : bad"):
        render_alembic_log(event="bad")


def test_install_postgres_cli_renders_alembic_log(capsys):
    assert main(["--alembic-log", "--event", "unknown-action", "--action", "bad"]) == 0

    assert capsys.readouterr().out == "ERROR:Action Alembic PostgreSQL inconnue : bad\n"


def test_render_sqlite_migration_log_for_known_events():
    assert render_sqlite_migration_log(event="detected", sqlite_db="/app/instance/transcrIA.db") == (
        "INFO:Base SQLite détectée : /app/instance/transcrIA.db\n"
    )
    assert render_sqlite_migration_log(event="skipped", sqlite_db="/app/instance/transcrIA.db") == (
        "INFO:Migration sautée (--pg-migrate absent)\n"
    )
    assert render_sqlite_migration_log(event="ignored", sqlite_db="/app/instance/transcrIA.db") == (
        "INFO:Migration ignorée — PG reste vide, /app/instance/transcrIA.db conservé\n"
    )
    assert render_sqlite_migration_log(event="unknown-action", sqlite_db="/app/instance/transcrIA.db", action="bad") == (
        "ERROR:Action de migration SQLite inconnue : bad\n"
    )
    assert render_sqlite_migration_log(
        event="backup-error",
        sqlite_db="/app/instance/transcrIA.db",
        backup_path="/app/backups/transcrIA.db.bak",
    ) == "ERROR:Échec du backup SQLite : /app/instance/transcrIA.db → /app/backups/transcrIA.db.bak\n"
    assert render_sqlite_migration_log(
        event="backup-ok",
        sqlite_db="/app/instance/transcrIA.db",
        backup_path="/app/backups/transcrIA.db.bak",
    ) == "OK:Backup SQLite sauvegardé : /app/backups/transcrIA.db.bak\n"
    assert render_sqlite_migration_log(event="migrate-start", sqlite_db="/app/instance/transcrIA.db") == (
        "INFO:Migration des données SQLite → PostgreSQL…\n"
    )
    assert render_sqlite_migration_log(event="migrate-ok", sqlite_db="/app/instance/transcrIA.db") == "OK:Données migrées\n"
    assert render_sqlite_migration_log(event="migrate-failed", sqlite_db="/app/instance/transcrIA.db") == (
        "ERROR:Échec de la migration SQLite → PostgreSQL\n"
    )
    assert render_sqlite_migration_log(event="migrate-partial", sqlite_db="/app/instance/transcrIA.db") == (
        "WARN:La base PostgreSQL est peut-être partiellement remplie. "
        "Utilisez --truncate pour recommencer ou nettoyez la base PG manuellement.\n"
    )


def test_render_sqlite_migration_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement de migration SQLite inconnu : bad"):
        render_sqlite_migration_log(event="bad", sqlite_db="/app/instance/transcrIA.db")


def test_render_sqlite_migration_prompt_is_stable():
    assert render_sqlite_migration_prompt(
        sqlite_db="/app/instance/transcrIA.db",
        sqlite_size="2.0K",
        db="transcria",
        host="127.0.0.1",
        port="5432",
    ) == (
        "\n"
        "=== Migration SQLite → PostgreSQL ===\n"
        "  Source : /app/instance/transcrIA.db (2.0K)\n"
        "  Cible  : transcria@127.0.0.1:5432\n"
        "\n"
        "Options :\n"
        "  1. Migrer les données SQLite (conservation locale + copie PG)\n"
        "  2. Ignorer (démarre avec une base PostgreSQL vide, laisse SQLite intact)\n"
        "  Votre choix [1/2] : "
    )


def test_install_postgres_cli_renders_sqlite_migration_prompt(capsys):
    assert main([
        "--sqlite-migration-prompt",
        "--sqlite-db", "/app/instance/transcrIA.db",
        "--sqlite-size", "2.0K",
        "--db", "transcria",
        "--host", "127.0.0.1",
        "--port", "5432",
    ]) == 0

    assert capsys.readouterr().out.endswith("  Votre choix [1/2] : ")


def test_render_database_setup_log_for_sqlite_and_postgres_events():
    assert render_database_setup_log(event="sqlite-kept") == "OK:Base SQLite conservée (storage.database_url de config.yaml)\n"
    assert render_database_setup_log(event="password-generated", user="app") == "INFO:Mot de passe du rôle 'app' généré automatiquement.\n"
    assert render_database_setup_log(event="configured", db="transcria", host="127.0.0.1", port="5432") == (
        "VALUE:PostgreSQL (transcria@127.0.0.1:5432)\n"
    )
    assert render_database_setup_log(event="config-failed") == "ERROR:PostgreSQL demandé mais la configuration a échoué.\n"


def test_render_database_setup_log_for_missing_requirements():
    assert render_database_setup_log(event="psql-missing") == (
        "ERROR:psql introuvable — PostgreSQL n'est pas installé.\n"
        "WARN:  Fedora/RHEL  : sudo dnf install postgresql-server postgresql && "
        "sudo postgresql-setup --initdb && sudo systemctl enable --now postgresql\n"
        "WARN:  Debian/Ubuntu: sudo apt install postgresql && sudo systemctl enable --now postgresql\n"
        "ERROR:PostgreSQL demandé : arrêt au lieu de poursuivre silencieusement en SQLite.\n"
    )
    assert render_database_setup_log(event="sudo-missing") == (
        "ERROR:sudo requis pour créer le rôle/la base PostgreSQL (compte postgres).\n"
        "ERROR:PostgreSQL demandé : arrêt au lieu de poursuivre silencieusement en SQLite.\n"
    )


def test_render_database_setup_log_rejects_unknown_event():
    with pytest.raises(ValueError, match="événement de choix base de données inconnu : bad"):
        render_database_setup_log(event="bad")


def test_install_postgres_cli_renders_database_setup_log(capsys):
    assert main(["--database-setup-log", "--event", "configured", "--db", "transcria", "--host", "db.internal", "--port", "5432"]) == 0

    assert capsys.readouterr().out == "VALUE:PostgreSQL (transcria@db.internal:5432)\n"


def test_rewrite_pg_hba_replaces_only_local_tcp_peer_and_ident():
    content = """# TYPE DATABASE USER ADDRESS METHOD
local all all peer
host all all 127.0.0.1/32 peer
host all all ::1/128 ident
host replication all 127.0.0.1/32 peer
host replication all ::1/128 ident # comment
host all all 10.0.0.0/8 peer
host all all 127.0.0.1/32 scram-sha-256
"""

    updated, changed = rewrite_pg_hba_for_tcp_password(content)

    assert changed == 4
    assert "local all all peer" in updated
    assert "host all all 127.0.0.1/32 scram-sha-256\n" in updated
    assert "host all all ::1/128 scram-sha-256\n" in updated
    assert "host replication all 127.0.0.1/32 scram-sha-256\n" in updated
    assert "host replication all ::1/128 scram-sha-256 # comment\n" in updated
    assert "host all all 10.0.0.0/8 peer" in updated
    assert updated.count("host all all 127.0.0.1/32 scram-sha-256") == 2


def test_rewrite_pg_hba_preserves_crlf():
    updated, changed = rewrite_pg_hba_for_tcp_password("host all all 127.0.0.1/32 peer\r\n")

    assert changed == 1
    assert updated == "host all all 127.0.0.1/32 scram-sha-256\r\n"


def test_rewrite_pg_hba_file_is_idempotent_and_preserves_mode(tmp_path):
    pg_hba = tmp_path / "pg_hba.conf"
    pg_hba.write_text("host all all 127.0.0.1/32 peer\n", encoding="utf-8")
    pg_hba.chmod(0o640)

    assert rewrite_pg_hba_file(pg_hba) == 1
    assert pg_hba.read_text(encoding="utf-8") == "host all all 127.0.0.1/32 scram-sha-256\n"
    assert stat.S_IMODE(os.stat(pg_hba).st_mode) == 0o640

    assert rewrite_pg_hba_file(pg_hba) == 0
    assert pg_hba.read_text(encoding="utf-8") == "host all all 127.0.0.1/32 scram-sha-256\n"
