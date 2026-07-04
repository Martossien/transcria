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
  {facts_rows}
</table>
<p style="margin:0 0 24px;text-align:center;">
  <a href="{job_url}"
     style="display:inline-block;background:#2563eb;color:#ffffff;
            text-decoration:none;padding:12px 28px;border-radius:6px;
            font-size:15px;font-weight:bold;">
    Voir les livrables &rarr;
  </a>
</p>"""

_BODY_SUMMARY_READY = """\
<p style="margin:0 0 16px;color:#333333;font-size:15px;">
  Bonjour {display_name},
</p>
<p style="margin:0 0 24px;color:#333333;font-size:15px;">
  La <strong>pré-analyse de votre audio est prête</strong>. À vous de jouer : vérifiez le
  contexte de la réunion, puis lancez le traitement final.
</p>
<table width="100%" cellpadding="0" cellspacing="0"
       style="background:#f8f9fa;border-radius:6px;margin-bottom:24px;">
  {facts_rows}
</table>
<p style="margin:0 0 24px;text-align:center;">
  <a href="{job_url}"
     style="display:inline-block;background:#2563eb;color:#ffffff;
            text-decoration:none;padding:12px 28px;border-radius:6px;
            font-size:15px;font-weight:bold;">
    Vérifier et lancer le traitement &rarr;
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

_BODY_VRAM_WAIT = """\
<p style="margin:0 0 16px;color:#333333;font-size:15px;">
  Bonjour {display_name},
</p>
<p style="margin:0 0 24px;color:#333333;font-size:15px;">
  Un traitement est <strong>en attente de VRAM</strong> : la mémoire GPU disponible est
  insuffisante pour la phase <strong>{phase}</strong> ({required_mb}&nbsp;Mo requis).
  Le job n'a <strong>pas</strong> échoué — il reprendra automatiquement dès que la VRAM
  sera libérée. Libérez de la mémoire GPU (arrêt d'une LLM, fin d'un autre traitement)
  pour accélérer la reprise.
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
     style="display:inline-block;background:#d97706;color:#ffffff;
            text-decoration:none;padding:12px 28px;border-radius:6px;
            font-size:15px;font-weight:bold;">
    Voir le traitement &rarr;
  </a>
</p>"""


def _facts_rows_html(facts: list[tuple[str, str]]) -> str:
    """Lignes d'un tableau de faits (label + valeur) pour le corps HTML des emails."""
    import html as html_mod

    rows = []
    for label, value in facts:
        rows.append(
            '<tr><td style="padding:14px 20px;border-top:1px solid #eeeeee;">'
            f'<p style="margin:0 0 4px;color:#666666;font-size:12px;text-transform:uppercase;'
            f'letter-spacing:0.5px;">{html_mod.escape(str(label))}</p>'
            f'<p style="margin:0;color:#111111;font-size:16px;font-weight:bold;">'
            f'{html_mod.escape(str(value))}</p></td></tr>'
        )
    return "".join(rows)


def _build_html_success(display_name: str, job_title: str, job_id: str, base_url: str,
                        facts: list[tuple[str, str]] | None = None) -> str:
    # Sur un job TERMINÉ, on pointe /result (livrables) plutôt que le wizard.
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/result"
    rows = _facts_rows_html([("Travail", job_title), *(facts or [])])
    body = _BODY_SUCCESS.format(display_name=display_name, facts_rows=rows, job_url=job_url)
    return _HTML_BASE.format(
        subject=f"Transcription terminée : {job_title}",
        header_bg="#16a34a",
        header_icon="✓",
        header_title="Transcription terminée",
        body_html=body,
    )


def _build_text_success(display_name: str, job_title: str, job_id: str, base_url: str,
                        facts: list[tuple[str, str]] | None = None) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/result"
    lines = [f"Bonjour {display_name},", "",
             "Votre transcription est terminée avec succès.", "",
             f"Travail : {job_title}"]
    for label, value in (facts or []):
        lines.append(f"{label} : {value}")
    lines += [f"Lien    : {job_url}", "", "Cet email a été envoyé automatiquement par TranscrIA."]
    return "\n".join(lines)


def _build_html_summary_ready(display_name: str, job_title: str, job_id: str, base_url: str,
                              facts: list[tuple[str, str]] | None = None) -> str:
    # Pré-analyse prête : on pointe le wizard (l'utilisateur doit valider le contexte).
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    rows = _facts_rows_html([("Travail", job_title), *(facts or [])])
    body = _BODY_SUMMARY_READY.format(display_name=display_name, facts_rows=rows, job_url=job_url)
    return _HTML_BASE.format(
        subject=f"Pré-analyse prête : {job_title}",
        header_bg="#2563eb",
        header_icon="✓",
        header_title="Pré-analyse prête — à vous de jouer",
        body_html=body,
    )


def _build_text_summary_ready(display_name: str, job_title: str, job_id: str, base_url: str,
                              facts: list[tuple[str, str]] | None = None) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    lines = [f"Bonjour {display_name},", "",
             "La pré-analyse de votre audio est prête. Vérifiez le contexte de la réunion,",
             "puis lancez le traitement final.", "",
             f"Travail : {job_title}"]
    for label, value in (facts or []):
        lines.append(f"{label} : {value}")
    lines += [f"Lien    : {job_url}", "", "Cet email a été envoyé automatiquement par TranscrIA."]
    return "\n".join(lines)


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


def _build_html_vram_wait(
    display_name: str, job_title: str, job_id: str, required_mb: int, phase: str, base_url: str
) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    subject = f"En attente de VRAM : {job_title}"
    body = _BODY_VRAM_WAIT.format(
        display_name=display_name,
        job_title=job_title,
        job_url=job_url,
        required_mb=required_mb,
        phase=phase,
    )
    return _HTML_BASE.format(
        subject=subject,
        header_bg="#d97706",
        header_icon="⏳",
        header_title="Traitement en attente de VRAM",
        body_html=body,
    )


def _build_text_vram_wait(
    display_name: str, job_title: str, job_id: str, required_mb: int, phase: str, base_url: str
) -> str:
    job_url = f"{base_url.rstrip('/')}/jobs/{job_id}/wizard"
    return (
        f"Bonjour {display_name},\n\n"
        f"Un traitement est en attente de VRAM (mémoire GPU insuffisante pour la phase "
        f"{phase}, {required_mb} Mo requis).\n"
        f"Le job n'a pas échoué : il reprendra automatiquement dès que la VRAM sera libérée.\n\n"
        f"Travail : {job_title}\n"
        f"Lien    : {job_url}\n\n"
        "Cet email a été envoyé automatiquement par TranscrIA."
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
    facts: list[tuple[str, str]] | None = None,
) -> None:
    """Lance en tâche de fond l'envoi d'un email de notification.

    Args:
        cfg: config applicative complète (get_config()).
        to_email: adresse du destinataire.
        display_name: nom affiché du destinataire.
        job_title: titre du job.
        job_id: identifiant du job.
        event: "summary_ready" (pré-analyse prête, à valider), "completed" ou "failed".
        error: message d'erreur (event="failed" uniquement).
        facts: lignes (label, valeur) affichées dans le corps — type détecté, locuteurs,
            durée, temps estimé/réel, score qualité selon l'événement.
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

    if event == "summary_ready":
        subject = f"[TranscrIA] Pré-analyse prête : {job_title}"
        html = _build_html_summary_ready(name, job_title, job_id, ecfg.base_url, facts)
        text = _build_text_summary_ready(name, job_title, job_id, ecfg.base_url, facts)
    elif event == "completed":
        subject = f"[TranscrIA] Transcription terminée : {job_title}"
        html = _build_html_success(name, job_title, job_id, ecfg.base_url, facts)
        text = _build_text_success(name, job_title, job_id, ecfg.base_url, facts)
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


def send_admin_vram_alert_async(
    cfg: dict,
    admin_emails: list[str],
    job_title: str,
    job_id: str,
    required_mb: int,
    phase: str,
) -> None:
    """Alerte les administrateurs qu'un job est en attente de VRAM (tâche de fond).

    Best-effort : aucune exception remontée. Ignoré si l'email est désactivé, mal
    configuré, ou si aucune adresse admin n'est fournie.
    """
    ecfg = build_email_config(cfg)
    recipients = [e for e in dict.fromkeys(admin_emails or []) if e]
    if not ecfg.enabled:
        return
    if not recipients:
        logger.debug("Alerte VRAM admin ignorée: aucun email admin (job=%s)", job_id)
        return
    if not ecfg.smtp_host or not ecfg.from_address:
        logger.warning("Alerte VRAM admin ignorée: SMTP non configuré (job=%s)", job_id)
        return

    subject = f"[TranscrIA] En attente de VRAM : {job_title}"

    def _do_send() -> None:
        for to_email in recipients:
            name = to_email.split("@")[0]
            html = _build_html_vram_wait(name, job_title, job_id, required_mb, phase, ecfg.base_url)
            text = _build_text_vram_wait(name, job_title, job_id, required_mb, phase, ecfg.base_url)
            try:
                _send_smtp(ecfg, to_email, subject, html, text)
                logger.info("Alerte VRAM admin envoyée: job=%s to=%s", job_id, to_email)
            except Exception:
                logger.exception("Échec alerte VRAM admin: job=%s to=%s", job_id, to_email)

    t = threading.Thread(target=_do_send, daemon=True, name=f"mailer-vram-{job_id[:8]}")
    t.start()
