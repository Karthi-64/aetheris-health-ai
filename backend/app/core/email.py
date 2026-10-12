"""Outbound email transport.

One small interface, :class:`EmailSender`, and one implementation over SMTP.
The Notifications worker is the only caller; nothing else in the application
sends mail directly (``docs/modules/11-notifications.md`` §5).

**Email is off unless ``SMTP_HOST`` is set.** :func:`get_email_sender` returns
``None`` in that case, and the worker records each queued email as failed with
a reason rather than attempting it. A development database is seeded with
staff addresses on a domain that is not reserved for testing, so "no transport
configured" has to mean "nothing leaves this process", not "try localhost".
"""

from __future__ import annotations

from dataclasses import dataclass
from email.message import EmailMessage as MimeMessage
from typing import Protocol, runtime_checkable

import aiosmtplib

from app.core.config import settings

__all__ = [
    "EmailMessage",
    "EmailSender",
    "SmtpEmailSender",
    "email_delivery_configured",
    "get_email_sender",
]

#: Seconds to wait on the mail server before giving up on one attempt. A slow
#: server is retried later by the queue; it must not stall the worker.
_SMTP_TIMEOUT_SECONDS = 15


@dataclass(frozen=True, slots=True)
class EmailMessage:
    """One plain-text email.

    :param to: Recipient address.
    :param subject: Subject line.
    :param body: Plain-text body.
    """

    to: str
    subject: str
    body: str


@runtime_checkable
class EmailSender(Protocol):
    """Something that can send an email."""

    async def send(self, message: EmailMessage) -> None:
        """Send one message.

        :param message: The email to send.
        :raises Exception: If the server refuses it or cannot be reached. The
            caller records the failure and retries.
        """
        ...


class SmtpEmailSender:
    """Sends email through an SMTP server.

    :param host: SMTP server host.
    :param port: SMTP server port.
    :param username: Username, if the server needs one.
    :param password: Password, if the server needs one.
    :param start_tls: Upgrade the connection with STARTTLS.
    :param sender: The From address.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        username: str | None,
        password: str | None,
        start_tls: bool,
        sender: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._start_tls = start_tls
        self._sender = sender

    async def send(self, message: EmailMessage) -> None:
        """Send one message over SMTP.

        :param message: The email to send.
        """
        mime = MimeMessage()
        mime["From"] = self._sender
        mime["To"] = message.to
        mime["Subject"] = message.subject
        mime.set_content(message.body)

        await aiosmtplib.send(
            mime,
            hostname=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            start_tls=self._start_tls,
            timeout=_SMTP_TIMEOUT_SECONDS,
        )


def email_delivery_configured() -> bool:
    """Whether this deployment has a mail transport at all.

    A credential that can only be delivered by email — an invitation, a
    password reset — must not be minted when this is false: there would be
    nowhere to send it, and nothing else is allowed to carry it.

    It says the transport is *configured*, not that a given message will
    arrive.

    :returns: ``True`` when ``SMTP_HOST`` is set.
    """
    return bool(settings.SMTP_HOST and settings.SMTP_HOST.strip())


def get_email_sender() -> EmailSender | None:
    """Return the configured email transport, or ``None`` if email is off.

    :returns: An SMTP sender when ``SMTP_HOST`` is set, otherwise ``None``.
    """
    if not email_delivery_configured() or settings.SMTP_HOST is None:
        return None
    return SmtpEmailSender(
        host=settings.SMTP_HOST,
        port=settings.SMTP_PORT,
        username=settings.SMTP_USER,
        password=settings.SMTP_PASSWORD,
        start_tls=settings.SMTP_STARTTLS,
        sender=settings.EMAIL_FROM,
    )
