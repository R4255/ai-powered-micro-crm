"""Email sending behind a small interface.

Default provider is "outbox": emails are stored in the local `outbox` table and
nothing leaves the machine. To send for real later, implement a provider below
(SMTP, SendGrid, Gmail API, ...) and set EMAIL_PROVIDER in .env.
"""
from datetime import datetime

from . import db
from .config import EMAIL_FROM, EMAIL_PROVIDER


class EmailError(RuntimeError):
    pass


def _send_outbox(to: str, subject: str, body: str) -> str:
    # Local only: the "sent" email just lands in the outbox table.
    return "saved_to_outbox"


def _send_smtp(to: str, subject: str, body: str) -> str:
    # Placeholder. To enable, fill SMTP_* in .env and implement with smtplib, e.g.:
    #   msg = EmailMessage(); msg["From"], msg["To"], msg["Subject"] = EMAIL_FROM, to, subject
    #   msg.set_content(body)
    #   with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s: s.starttls(); s.login(...); s.send_message(msg)
    raise EmailError("SMTP sending is not implemented yet. Set EMAIL_PROVIDER=outbox.")


PROVIDERS = {"outbox": _send_outbox, "smtp": _send_smtp}


def provider_name() -> str:
    return EMAIL_PROVIDER if EMAIL_PROVIDER in PROVIDERS else "outbox"


def send(customer_id: str, to: str, subject: str, body: str) -> dict:
    status = PROVIDERS[provider_name()](to, subject, body)
    created_at = datetime.now().isoformat(timespec="seconds")
    db.execute(
        "INSERT INTO outbox (customer_id, to_email, from_email, subject, body, provider, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        customer_id, to, EMAIL_FROM, subject, body, provider_name(), status, created_at,
    )
    return {"provider": provider_name(), "status": status, "to": to}
