"""Envoi d'emails de notification pour les événements jobs (succès / échec)."""

from __future__ import annotations

import logging
import smtplib
import ssl
import threading
from dataclasses import dataclass, fields
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


@dataclass
class EmailConfig:
    enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    use_starttls: bool = True
    use_ssl: bool = False
    from_address: str = ""
    from_name: str = "TranscrIA"
    base_url: str = "http://localhost:7870"


def build_email_config(cfg: dict) -> EmailConfig:
    notif = cfg.get("notifications", {}).get("email", {})
    known = {f.name for f in fields(EmailConfig)}
    return EmailConfig(**{k: v for k, v in notif.items() if k in known})


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------

_HTML_BASE = """\
<!DOCTYPE html>
<html lang="fr">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{subject}</title>
</head>
<body style="margin:0;padding:0;background:#f5f5f5;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:32px 0;">
    <tr><td align="center">
      <table width="600" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:8px;overflow:hidden;
                    box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <!-- Header -->
        <tr>
          <td style="background:{header_bg};padding:24px 32px;">
            <h1 style="margin:0;color:#ffffff;font-size:20px;font-weight:bold;">
              {header_icon}&nbsp; {header_title}
            </h1>
          </td>
        </tr>
        <!-- Body -->
        <tr>
          <td style="padding:32px;">
            {body_html}
          </td>
        </tr>
        <!-- Footer -->
        <tr>
          <td style="padding:16px 32px 24px;border-top:1px solid #eeeeee;">
            <p style="margin:0;color:#999999;font-size:12px;">
              Cet email a été envoyé automatiquement par TranscrIA.<br>
              Vous recevez ce message car vous êtes propriétaire d'un travail de transcription.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""

_BODY_SUCCESS = """\
<p style="margin:0 0 16px;color:#333333;font-size:15px;">
  Bonjour {display_name},
</p>
<p style="margin:0 0 24px;color:#333333;font-size:15px;">
  Votre transcription est <strong>terminée avec succès</strong>.
</p>
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f8f9fa;border-radius:6px;margin-bottom:24px;">
  <tr>
    <td style="padding:16px 20px;">
      <p style="margin:0 0 6px;color:#666666;font-size:12px;text-transform:uppercase;
                letter-spacing:0.5px;">Travail</p>
      <p style="margin:0;color:#111111;font-size:16px;font-weight:bold;">{job_title}</p>
    </td>
  </tr>
</table>
<p style="margin:0 0 24px;text-align:center;">
  <a href="{job_url}"
     style="display:inline-block;background:#2563eb;color:#ffffff;
            text-decoration:none;padding:12px 28px;border-radius:6px;
            font-size:15px;font-weight:bold;">
    Voir la transcription &rarr;
  </a>
</p>"""

_BODY_FAILURE = """\
<p style="margin:0 0 16px;color:#333333;font-size:15px;">
  Bonjour {display_name},
</p>
<p style="margin:0 0 24px;color:#333333;font-size:15px;">
  Votre transcription a <strong>échoué</strong>.
</p>
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f8f9fa;border-radius:6px;margin-bottom:16px;">
  <tr>
    <td style="padding:16px 20px;">
      <p style="margin:0 0 6px;color:#666666;font-size:12px;text-transform:uppercase;
                letter-spacing:0.5px;">Travail</p>
      <p style="margin:0;color:#111111;font-size:16px;font-weight:bold;">{job_title}</p>
    </td>
  </tr>
</table>
{error_block}
<p style="margin:16px 0 24px;text-align:center;">
  <a href="{job_url}"
     style="display:inline-block;background:#dc2626;color:#ffffff;
            text-decoration:none;padding:12px 28px;border-radius:6px;
            font-size:15px;font-weight:bold;">
    Voir le détail &rarr;
  </a>
</p>"""

_ERROR_BLOCK = """\
<div style="background:#fef2f2;border:1px solid #fecaca;border-radius:6px;
            padding:12px 16px;margin-bottom:16px;">
  <p style="margin:0 0 4px;color:#dc2626;font-size:12px;font-weight:bold;
            text-transform:uppercase;letter-spacing:0.5px;">Erreur</p>
  <p style="margin:0;color:#7f1d1d;font-size:13px;font-family:monospace;
            word-break:break-all;">{error}</p>
</div>"""


def _build_html_success(display_name: str, job_title: str, job_id: str, base_url: str) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    subject = f"Transcription terminée : {job_title}"
    body = _BODY_SUCCESS.format(display_name=display_name, job_title=job_title, job_url=job_url)
    return _HTML_BASE.format(
        subject=subject,
        header_bg="#16a34a",
        header_icon="✓",
        header_title="Transcription terminée",
        body_html=body,
    )


def _build_text_success(display_name: str, job_title: str, job_id: str, base_url: str) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    return (
        f"Bonjour {display_name},\n\n"
        f"Votre transcription est terminée avec succès.\n\n"
        f"Travail : {job_title}\n"
        f"Lien    : {job_url}\n\n"
        "Cet email a été envoyé automatiquement par TranscrIA."
    )


def _build_html_failure(
    display_name: str, job_title: str, job_id: str, error: str, base_url: str
) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    subject = f"Échec de transcription : {job_title}"
    import html as html_mod
    error_safe = html_mod.escape(error) if error else ""
    error_block = _ERROR_BLOCK.format(error=error_safe) if error_safe else ""
    body = _BODY_FAILURE.format(
        display_name=display_name,
        job_title=job_title,
        job_url=job_url,
        error_block=error_block,
    )
    return _HTML_BASE.format(
        subject=subject,
        header_bg="#dc2626",
        header_icon="✗",
        header_title="Échec de transcription",
        body_html=body,
    )


def _build_text_failure(
    display_name: str, job_title: str, job_id: str, error: str, base_url: str
) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    lines = [
        f"Bonjour {display_name},",
        "",
        "Votre transcription a échoué.",
        "",
        f"Travail : {job_title}",
        f"Lien    : {job_url}",
    ]
    if error:
        lines += ["", f"Erreur  : {error}"]
    lines += ["", "Cet email a été envoyé automatiquement par TranscrIA."]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SMTP send
# ---------------------------------------------------------------------------

def _send_smtp(ecfg: EmailConfig, to: str, subject: str, html: str, text: str) -> None:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = (
        f"{ecfg.from_name} <{ecfg.from_address}>" if ecfg.from_name else ecfg.from_address
    )
    msg["To"] = to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(html, "html", "utf-8"))

    if ecfg.use_ssl:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(ecfg.smtp_host, ecfg.smtp_port, context=ctx) as srv:
            if ecfg.smtp_username:
                srv.login(ecfg.smtp_username, ecfg.smtp_password)
            srv.sendmail(ecfg.from_address, to, msg.as_bytes())
    elif ecfg.use_starttls:
        with smtplib.SMTP(ecfg.smtp_host, ecfg.smtp_port) as srv:
            srv.ehlo()
            srv.starttls()
            srv.ehlo()
            if ecfg.smtp_username:
                srv.login(ecfg.smtp_username, ecfg.smtp_password)
            srv.sendmail(ecfg.from_address, to, msg.as_bytes())
    else:
        with smtplib.SMTP(ecfg.smtp_host, ecfg.smtp_port) as srv:
            if ecfg.smtp_username:
                srv.login(ecfg.smtp_username, ecfg.smtp_password)
            srv.sendmail(ecfg.from_address, to, msg.as_bytes())


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def send_job_notification_async(
    cfg: dict,
    to_email: str,
    display_name: str,
    job_title: str,
    job_id: str,
    event: str,
    error: str | None = None,
) -> None:
    """Lance en tâche de fond l'envoi d'un email de notification.

    Args:
        cfg: config applicative complète (get_config()).
        to_email: adresse du destinataire.
        display_name: nom affiché du destinataire.
        job_title: titre du job.
        job_id: identifiant du job.
        event: "completed" ou "failed".
        error: message d'erreur (event="failed" uniquement).
    """
    ecfg = build_email_config(cfg)
    if not ecfg.enabled:
        return
    if not to_email:
        logger.debug("Notification ignorée: pas d'email configuré pour ce destinataire (job=%s)", job_id)
        return
    if not ecfg.smtp_host:
        logger.warning("Notification email ignorée: smtp_host non configuré (job=%s)", job_id)
        return
    if not ecfg.from_address:
        logger.warning("Notification email ignorée: from_address non configuré (job=%s)", job_id)
        return

    name = display_name or to_email.split("@")[0]

    if event == "completed":
        subject = f"[TranscrIA] Transcription terminée : {job_title}"
        html = _build_html_success(name, job_title, job_id, ecfg.base_url)
        text = _build_text_success(name, job_title, job_id, ecfg.base_url)
    else:
        subject = f"[TranscrIA] Échec de transcription : {job_title}"
        html = _build_html_failure(name, job_title, job_id, error or "", ecfg.base_url)
        text = _build_text_failure(name, job_title, job_id, error or "", ecfg.base_url)

    def _do_send() -> None:
        try:
            _send_smtp(ecfg, to_email, subject, html, text)
            logger.info(
                "Notification email envoyée: job=%s event=%s to=%s", job_id, event, to_email
            )
        except Exception:
            logger.exception(
                "Échec envoi notification email: job=%s event=%s to=%s", job_id, event, to_email
            )

    t = threading.Thread(target=_do_send, daemon=True, name=f"mailer-{job_id[:8]}-{event}")
    t.start()
