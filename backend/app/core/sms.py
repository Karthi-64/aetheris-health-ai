"""Outbound SMS transport.

One small interface, :class:`SmsSender`, and a factory, in the shape of
:mod:`app.core.email`. The Patient App's one-time sign-in code is the only
message sent today (``docs/modules/15-patient-app.md`` §5.9).

**SMS is off unless ``SMS_PROVIDER`` is set.** :func:`get_sms_sender` returns
``None`` in that case and the caller refuses the request; nothing ever
pretends a message was sent.

**No provider is chosen yet**, so there is no vendor adapter. The only adapter
is :class:`DevSmsSender`, which writes the message to this process's standard
output so a developer can sign in. It is the single, bounded exception to "a
code is never written anywhere", and it cannot be selected outside
development: ``app/core/config.py`` refuses to start with it, and the factory
refuses to build it. When a provider is approved it is one class implementing
:class:`SmsSender` over ``httpx``, one entry in :func:`get_sms_sender` and its
name in ``KNOWN_SMS_PROVIDERS``.

Nothing here logs :attr:`SmsMessage.body`, and :class:`SmsDeliveryError`
carries one fixed message: a provider's own error text can quote the request
it rejected.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from app.core.config import SMS_PROVIDER_DEV, settings

__all__ = [
    "DevSmsSender",
    "SmsDeliveryError",
    "SmsMessage",
    "SmsSender",
    "get_sms_sender",
]


class SmsDeliveryError(Exception):
    """An SMS could not be handed to the provider.

    The message is fixed. It never contains the recipient, the body or
    anything the provider said.
    """

    def __init__(self) -> None:
        super().__init__("The SMS could not be delivered.")


@dataclass(frozen=True, slots=True)
class SmsMessage:
    """One text message.

    :param to: Recipient number in E.164 form.
    :param body: The text. Kept out of ``repr`` so that logging a message, or
        an exception that holds one, cannot print a sign-in code.
    :param purpose: What the message is for. ``"otp"`` is the only value.
    """

    to: str
    body: str = field(repr=False)
    purpose: Literal["otp"] = "otp"


@runtime_checkable
class SmsSender(Protocol):
    """Something that can send a text message."""

    async def send(self, message: SmsMessage) -> None:
        """Send one message.

        :param message: The message to send.
        :raises SmsDeliveryError: On any failure, with no provider text.
        """
        ...


class DevSmsSender:
    """Writes the message to standard output instead of sending it.

    Development only. Standard output, not the logger: the application's log
    stream is shipped and retained, and a sign-in code must never be in it.
    """

    async def send(self, message: SmsMessage) -> None:
        """Print the message for the developer running the process.

        :param message: The message to "send".
        """
        sys.stdout.write(f"[dev sms] to={message.to} purpose={message.purpose}: {message.body}\n")
        sys.stdout.flush()


def get_sms_sender() -> SmsSender | None:
    """Return the configured SMS transport, or ``None`` if SMS is off.

    :returns: The adapter named by ``SMS_PROVIDER``; ``None`` when it is unset
        or names nothing this build can send with. The dev adapter is never
        returned outside development, whatever the settings object says.
    """
    provider = settings.SMS_PROVIDER
    if provider is None:
        return None
    if provider == SMS_PROVIDER_DEV and settings.is_development:
        return DevSmsSender()
    return None
