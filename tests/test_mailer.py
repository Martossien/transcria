"""Tests unitaires pour le module de notification email."""

import threading
import time
from types import SimpleNamespace
from unittest.mock import MagicMock, call, patch

import pytest

from transcria.notifications.mailer import (
    EmailConfig,
    _build_html_failure,
    _build_html_success,
    _build_text_failure,
    _build_text_success,
    build_email_config,
    send_job_notification_async,
)


# ---------------------------------------------------------------------------
# build_email_config
# ---------------------------------------------------------------------------

def test_build_email_config_returns_defaults_when_section_missing():
    ecfg = build_email_config({})
    assert ecfg.enabled is False
    assert ecfg.smtp_port == 587
    assert ecfg.use_starttls is True
    assert ecfg.use_ssl is False
    assert ecfg.from_name == "TranscrIA"


def test_build_email_config_picks_values_from_cfg():
    cfg = {
        "notifications": {
            "email": {
                "enabled": True,
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "smtp_username": "user@example.com",
                "smtp_password": "s3cr3t",
                "use_starttls": False,
                "use_ssl": True,
                "from_address": "transcria@example.com",
                "from_name": "Mon TranscrIA",
                "base_url": "https://transcria.example.com",
            }
        }
    }
    ecfg = build_email_config(cfg)
    assert ecfg.enabled is True
    assert ecfg.smtp_host == "smtp.example.com"
    assert ecfg.smtp_port == 465
    assert ecfg.use_ssl is True
    assert ecfg.use_starttls is False
    assert ecfg.from_address == "transcria@example.com"
    assert ecfg.base_url == "https://transcria.example.com"


def test_build_email_config_ignores_unknown_keys():
    cfg = {"notifications": {"email": {"enabled": True, "unknown_key": "oops"}}}
    ecfg = build_email_config(cfg)
    assert ecfg.enabled is True
    assert not hasattr(ecfg, "unknown_key")


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

def test_build_html_success_contains_job_title_and_link():
    html = _build_html_success("Alice", "Réunion du lundi", "abc123", "http://localhost:7870")
    assert "Réunion du lundi" in html
    assert "http://localhost:7870/jobs/abc123/wizard" in html
    assert "terminée" in html.lower()
    assert "Alice" in html


def test_build_text_success_contains_all_fields():
    text = _build_text_success("Bob", "Conf annuelle", "xyz999", "https://tr.example.com")
    assert "Bob" in text
    assert "Conf annuelle" in text
    assert "https://tr.example.com/jobs/xyz999/wizard" in text
    assert "terminée" in text.lower()


def test_build_html_failure_contains_error_and_link():
    html = _build_html_failure("Carol", "Présentation Q4", "err001", "VRAM insuffisante", "http://host")
    assert "Présentation Q4" in html
    assert "VRAM insuffisante" in html
    assert "http://host/jobs/err001/wizard" in html
    assert "échoué" in html.lower() or "échec" in html.lower()


def test_build_html_failure_escapes_html_in_error():
    html = _build_html_failure("Dave", "Job", "j1", "<script>alert('xss')</script>", "http://h")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_text_failure_omits_error_block_when_empty():
    text = _build_text_failure("Eve", "Job", "j2", "", "http://h")
    assert "Erreur" not in text


def test_build_text_failure_includes_error_when_present():
    text = _build_text_failure("Eve", "Job", "j3", "Timeout LLM", "http://h")
    assert "Timeout LLM" in text


def test_base_url_trailing_slash_stripped():
    html = _build_html_success("Alice", "Job", "abc", "http://localhost:7870/")
    assert "http://localhost:7870/jobs/abc/wizard" in html


# ---------------------------------------------------------------------------
# send_job_notification_async — comportement d'envoi
# ---------------------------------------------------------------------------

def _cfg(enabled=True, host="smtp.test", from_addr="noreply@test.com"):
    return {
        "notifications": {
            "email": {
                "enabled": enabled,
                "smtp_host": host,
                "smtp_port": 587,
                "from_address": from_addr,
                "use_starttls": True,
                "use_ssl": False,
            }
        }
    }


def test_send_notification_disabled_does_nothing():
    with patch("transcria.notifications.mailer._send_smtp") as mock_smtp:
        send_job_notification_async(_cfg(enabled=False), "a@b.com", "A", "Job", "j1", "completed")
        time.sleep(0.05)
        mock_smtp.assert_not_called()


def test_send_notification_missing_email_does_nothing():
    with patch("transcria.notifications.mailer._send_smtp") as mock_smtp:
        send_job_notification_async(_cfg(), "", "A", "Job", "j1", "completed")
        time.sleep(0.05)
        mock_smtp.assert_not_called()


def test_send_notification_missing_smtp_host_does_nothing():
    with patch("transcria.notifications.mailer._send_smtp") as mock_smtp:
        send_job_notification_async(_cfg(host=""), "a@b.com", "A", "Job", "j1", "completed")
        time.sleep(0.05)
        mock_smtp.assert_not_called()


def test_send_notification_completed_calls_smtp():
    sent = threading.Event()
    captured = {}

    def fake_smtp(ecfg, to, subject, html, text):
        captured.update({"to": to, "subject": subject, "html": html, "text": text})
        sent.set()

    with patch("transcria.notifications.mailer._send_smtp", side_effect=fake_smtp):
        send_job_notification_async(_cfg(), "user@example.com", "Alice", "Réunion", "abc123", "completed")
        assert sent.wait(timeout=2), "Email non envoyé dans le délai"

    assert captured["to"] == "user@example.com"
    assert "terminée" in captured["subject"].lower() or "terminé" in captured["subject"].lower()
    assert "Réunion" in captured["subject"]
    assert "Alice" in captured["html"]
    assert "abc123" in captured["html"]


def test_send_notification_failed_includes_error():
    sent = threading.Event()
    captured = {}

    def fake_smtp(ecfg, to, subject, html, text):
        captured.update({"subject": subject, "html": html, "text": text})
        sent.set()

    with patch("transcria.notifications.mailer._send_smtp", side_effect=fake_smtp):
        send_job_notification_async(
            _cfg(), "user@example.com", "Bob", "Conf", "xyz", "failed", error="GPUSessionError"
        )
        assert sent.wait(timeout=2), "Email non envoyé dans le délai"

    assert "échec" in captured["subject"].lower() or "Échec" in captured["subject"]
    assert "GPUSessionError" in captured["html"]
    assert "GPUSessionError" in captured["text"]


def test_send_notification_smtp_error_does_not_propagate():
    """Une exception SMTP ne doit jamais remonter vers l'appelant."""
    done = threading.Event()

    def failing_smtp(*_args, **_kwargs):
        done.set()
        raise ConnectionRefusedError("SMTP down")

    with patch("transcria.notifications.mailer._send_smtp", side_effect=failing_smtp):
        # Ne doit pas lever
        send_job_notification_async(_cfg(), "a@b.com", "A", "Job", "j1", "completed")
        assert done.wait(timeout=2)


def test_send_notification_uses_display_name_fallback_from_email():
    """Si display_name est vide, utilise la partie locale de l'adresse."""
    sent = threading.Event()
    captured = {}

    def fake_smtp(ecfg, to, subject, html, text):
        captured["html"] = html
        sent.set()

    with patch("transcria.notifications.mailer._send_smtp", side_effect=fake_smtp):
        send_job_notification_async(_cfg(), "alice@example.com", "", "Job", "j1", "completed")
        assert sent.wait(timeout=2)

    assert "alice" in captured["html"]


# ---------------------------------------------------------------------------
# send_smtp — sélection du mode de connexion
# ---------------------------------------------------------------------------

def _ecfg_starttls():
    return EmailConfig(
        enabled=True, smtp_host="smtp.test", smtp_port=587,
        smtp_username="u", smtp_password="p",
        use_starttls=True, use_ssl=False,
        from_address="from@test.com",
    )


def _ecfg_ssl():
    return EmailConfig(
        enabled=True, smtp_host="smtp.test", smtp_port=465,
        smtp_username="u", smtp_password="p",
        use_starttls=False, use_ssl=True,
        from_address="from@test.com",
    )


def _ecfg_plain():
    return EmailConfig(
        enabled=True, smtp_host="smtp.test", smtp_port=25,
        smtp_username="", smtp_password="",
        use_starttls=False, use_ssl=False,
        from_address="from@test.com",
    )


def _make_smtp_mock():
    srv = MagicMock()
    srv.__enter__ = MagicMock(return_value=srv)
    srv.__exit__ = MagicMock(return_value=False)
    return srv


def test_send_smtp_uses_starttls():
    from transcria.notifications.mailer import _send_smtp
    srv = _make_smtp_mock()
    with patch("smtplib.SMTP", return_value=srv) as mock_smtp_cls:
        _send_smtp(_ecfg_starttls(), "to@test.com", "Subj", "<p>html</p>", "text")
        mock_smtp_cls.assert_called_once_with("smtp.test", 587)
        srv.starttls.assert_called_once()
        srv.login.assert_called_once_with("u", "p")
        srv.sendmail.assert_called_once()


def test_send_smtp_uses_ssl():
    from transcria.notifications.mailer import _send_smtp
    srv = _make_smtp_mock()
    with patch("smtplib.SMTP_SSL", return_value=srv) as mock_smtp_ssl_cls:
        with patch("ssl.create_default_context", return_value=MagicMock()):
            _send_smtp(_ecfg_ssl(), "to@test.com", "Subj", "<p>html</p>", "text")
            mock_smtp_ssl_cls.assert_called_once()
            srv.login.assert_called_once_with("u", "p")
            srv.sendmail.assert_called_once()


def test_send_smtp_plain_skips_login_when_no_credentials():
    from transcria.notifications.mailer import _send_smtp
    srv = _make_smtp_mock()
    with patch("smtplib.SMTP", return_value=srv):
        _send_smtp(_ecfg_plain(), "to@test.com", "Subj", "<p>html</p>", "text")
        srv.login.assert_not_called()
        srv.sendmail.assert_called_once()


class TestNotifyHook:
    """Hook _notify (job_executor) : robuste + traçable, jamais bloquant."""

    def test_notify_delegates_with_resolved_owner_email(self):
        from transcria.services import job_executor

        owner = SimpleNamespace(email="alice@test.com", display_name="Alice", username="alice")
        job = SimpleNamespace(id="j1", title="Réunion", owner=owner)
        with patch.object(job_executor, "send_job_notification_async") as send:
            job_executor._notify({"k": 1}, job, "completed", error=None)
        send.assert_called_once()
        kwargs = send.call_args.kwargs
        assert kwargs["to_email"] == "alice@test.com"
        assert kwargs["display_name"] == "Alice"
        assert kwargs["job_id"] == "j1"
        assert kwargs["event"] == "completed"

    def test_notify_never_raises_and_logs_when_owner_unresolvable(self):
        from transcria.services import job_executor

        class _DetachedJob:
            id = "job-x"
            title = "T"

            @property
            def owner(self):
                raise RuntimeError("instance détachée hors session")

        fake_log = MagicMock()
        with patch.object(job_executor, "get_structured_logger", return_value=fake_log):
            with patch.object(job_executor, "send_job_notification_async") as send:
                # ne doit jamais lever
                job_executor._notify({}, _DetachedJob(), "failed", error="boom")
        send.assert_not_called()
        fake_log.warning.assert_called_once()
        # l'échec est traçable : event et job_id présents dans le log
        assert "failed" in fake_log.warning.call_args.args
        assert "job-x" in fake_log.warning.call_args.args
