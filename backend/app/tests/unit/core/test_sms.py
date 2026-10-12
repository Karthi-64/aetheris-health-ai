"""Unit tests for :mod:`app.core.sms` — off unless configured, and never a code in a log."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.core import sms
from app.core.config import settings
from app.core.sms import DevSmsSender, SmsDeliveryError, SmsMessage, SmsSender, get_sms_sender

if TYPE_CHECKING:
    import pytest


class TestFactory:
    def test_sms_is_off_when_no_provider_is_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMS_PROVIDER", None)

        assert get_sms_sender() is None

    def test_the_dev_sender_is_built_in_development(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMS_PROVIDER", "dev")

        sender = get_sms_sender()

        assert isinstance(sender, DevSmsSender)
        assert isinstance(sender, SmsSender)

    def test_the_dev_sender_is_never_built_outside_development(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: a settings object changed after start-up. The factory checks again."""
        monkeypatch.setattr(settings, "SMS_PROVIDER", "dev")
        monkeypatch.setattr(settings, "APP_ENV", "production")

        assert get_sms_sender() is None

    def test_an_unknown_provider_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "SMS_PROVIDER", "somebody")

        assert get_sms_sender() is None


class TestMessage:
    def test_the_body_is_not_in_the_repr(self) -> None:
        """Attack: log the message object and read the code out of the log."""
        message = SmsMessage(to="+919800000000", body="704615 is your code")

        assert "704615" not in repr(message)
        assert "704615" not in str(message)
        assert message.purpose == "otp"

    def test_a_delivery_error_says_nothing_about_the_message(self) -> None:
        error = SmsDeliveryError()

        assert str(error) == "The SMS could not be delivered."
        assert error.args == ("The SMS could not be delivered.",)


class TestDevSender:
    async def test_it_writes_to_standard_output_and_not_to_the_log(
        self, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
    ) -> None:
        await DevSmsSender().send(SmsMessage(to="+919812345678", body="654321 is your code"))

        assert "654321" in capsys.readouterr().out
        assert "654321" not in caplog.text

    def test_the_module_logs_nothing_at_all(self) -> None:
        """No logger exists in the module, so no code path can log a body."""
        assert not hasattr(sms, "logger")
        assert not hasattr(sms, "_logger")
