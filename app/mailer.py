"""Plain-text transactional email over SMTP. Configured by SMTP_* env vars; a no-op when unset."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from app import config

log = logging.getLogger("searchify.mailer")


def configured() -> bool:
    return bool(config.SMTP_HOST)


def send(to: str, subject: str, body: str) -> bool:
    """True only when the SMTP server accepted the message."""
    if not configured() or not to:
        log.info("email not sent (SMTP not configured): %s -> %s", subject, to)
        return False
    msg = EmailMessage()
    msg["From"] = config.MAIL_FROM
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        if config.SMTP_TLS == "ssl":
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, context=ssl.create_default_context(), timeout=20)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=20)
            if config.SMTP_TLS == "starttls":
                server.starttls(context=ssl.create_default_context())
        with server:
            if config.SMTP_USER:
                server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.send_message(msg)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        log.warning("email failed: %s -> %s: %s", subject, to, exc)
        return False
