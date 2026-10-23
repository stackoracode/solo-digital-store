"""Transactional e-mail. Always sent from the job queue, never inline in a request."""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.core.i18n import translate
from app.core.money import fmt
from app.core.security import decrypt
from app.db import utcnow
from app.models.ops import EmailLog, Ticket
from app.models.orders import Order

log = logging.getLogger(__name__)


def _order_context(db: Session, order_id: int) -> tuple[str, str, dict[str, Any]]:
    from app.services.orders import order_link

    order = db.get(Order, order_id)
    assert order is not None
    title = order.items[0].snapshot.get("product_title", "") if order.items else ""
    return (
        order.email,
        order.locale,
        {
            "order_no": order.order_no,
            "total": fmt(order.total, order.currency),
            "product": title,
            "link": order_link(order),
        },
    )


def build_message(db: Session, template: str, payload: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Return (to, locale, subject, body) for a template, or None if not applicable."""
    if template.startswith("order_"):
        to, locale, ctx = _order_context(db, int(payload["order_id"]))
    elif template.startswith("ticket_"):
        from app.services.support import ticket_link

        ticket = db.get(Ticket, int(payload["ticket_id"]))
        if ticket is None:
            return None
        to, locale = ticket.email, "en-US"
        if ticket.order_id:
            order = db.get(Order, ticket.order_id)
            locale = order.locale if order else locale
        ctx = {"ticket_no": ticket.ticket_no, "subject": ticket.subject, "link": ticket_link(ticket)}
    elif template == "login_code":
        to, locale = payload["email"], payload.get("locale", "en-US")
        ctx = {"code": decrypt(payload["code_enc"]), "minutes": get_settings().email_code_ttl_minutes}
    elif template == "order_lookup":
        to, locale, ctx = _order_context(db, int(payload["order_id"]))
    else:
        return None
    subject = translate(locale, f"email.{template}.subject", **ctx)
    body = translate(locale, f"email.{template}.body", **ctx)
    return to, locale, subject, body


def send_email(db: Session, template: str, payload: dict[str, Any]) -> str:
    settings = get_settings()
    built = build_message(db, template, payload)
    if built is None:
        return "skipped"
    to, _locale, subject, body = built
    recent = (
        db.scalar(
            select(func.count())
            .select_from(EmailLog)
            .where(
                EmailLog.to_email == to,
                EmailLog.status == "sent",
                EmailLog.created_at > utcnow() - timedelta(hours=1),
            )
        )
        or 0
    )
    if recent >= settings.email_max_per_recipient_hour:
        db.add(
            EmailLog(
                created_at=utcnow(), to_email=to, template=template, status="skipped", error="rate_limited"
            )
        )
        db.commit()
        return "rate_limited"
    if not settings.smtp_host:
        log.info("smtp not configured; e-mail %s to %s not sent", template, to[:3] + "***")
        db.add(
            EmailLog(created_at=utcnow(), to_email=to, template=template, status="skipped", error="no_smtp")
        )
        db.commit()
        return "no_smtp"
    msg = EmailMessage()
    msg["From"] = settings.smtp_from
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    if settings.smtp_ssl:
        server: smtplib.SMTP = smtplib.SMTP_SSL(
            settings.smtp_host, settings.smtp_port, timeout=20, context=context
        )
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20)
    try:
        if settings.smtp_starttls and not settings.smtp_ssl:
            server.starttls(context=context)
        if settings.smtp_username:
            server.login(settings.smtp_username, settings.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:
            pass
    db.add(EmailLog(created_at=utcnow(), to_email=to, template=template, status="sent"))
    db.commit()
    return "sent"
