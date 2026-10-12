"""Unit tests for the outbound email transport.

No mail server is contacted: ``aiosmtplib.send`` is replaced, and the tests
assert on what it would have been asked to do.
"""

from __future__ import annotations

from typing import Any

import aiosmtplib
import pytest

from app.core.config import settings
from app.core.email import EmailMessage, EmailSender, SmtpEmailSender, get_email_sender


class TestGetEmailSender:
    def test_email_is_off_when_no_host_is_configured(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The safe default: nothing leaves the process.
        monkeypatch.setattr(settings, "SMTP_HOST", None)

        assert get_email_sender() is None

    def test_an_empty_host_also_means_off(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", "")

        assert get_email_sender() is None

    def test_a_configured_host_gives_an_smtp_sender(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.example.test")

        sender = get_email_sender()

        assert isinstance(sender, SmtpEmailSender)
        assert isinstance(sender, EmailSender)


class TestSmtpEmailSender:
    async def test_send_passes_the_message_and_settings_to_smtp(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, dict[str, Any]]] = []

        async def fake_send(message: Any, **kwargs: Any) -> None:
            calls.append((message, kwargs))

        monkeypatch.setattr(aiosmtplib, "send", fake_send)
        sender = SmtpEmailSender(
            host="smtp.example.test",
            port=2525,
            username="mailer",
            password="s3cret",  # noqa: S106 — a literal for the test double
            start_tls=True,
            sender="noreply@example.test",
        )

        await sender.send(
            EmailMessage(to="asha@example.test", subject="Welcome", body="Set your password.")
        )

        assert len(calls) == 1
        message, kwargs = calls[0]
        assert message["From"] == "noreply@example.test"
        assert message["To"] == "asha@example.test"
        assert message["Subject"] == "Welcome"
        assert message.get_content().strip() == "Set your password."
        assert kwargs["hostname"] == "smtp.example.test"
        assert kwargs["port"] == 2525
        assert kwargs["username"] == "mailer"
        assert kwargs["start_tls"] is True
        # A bounded wait, so a slow server cannot stall the worker.
        assert kwargs["timeout"] > 0

    async def test_a_transport_error_propagates_to_the_caller(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The queue records the failure and retries; the sender must not hide it.
        async def refuse(*_args: Any, **_kwargs: Any) -> None:
            msg = "connection refused"
            raise ConnectionRefusedError(msg)

        monkeypatch.setattr(aiosmtplib, "send", refuse)
        sender = SmtpEmailSender(
            host="smtp.example.test",
            port=25,
            username=None,
            password=None,
            start_tls=False,
            sender="noreply@example.test",
        )

        with pytest.raises(ConnectionRefusedError):
            await sender.send(EmailMessage(to="a@example.test", subject="s", body="b"))
