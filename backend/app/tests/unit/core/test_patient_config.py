"""Patient App settings: nothing unsafe starts outside development.

Each test attacks the configuration: every way of ending up with a code hashed
under no key, a sign-in code printed in a log, a cookie sent in the clear, or a
code sent to any country must stop the application from starting.

No database.
"""

from __future__ import annotations

import os
from typing import Any

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr, ValidationError

from app.core.config import MIN_PATIENT_OTP_SECRET_LENGTH, Settings

SIGNING_KEY = "a-private-signing-key-for-these-tests-0123456789"
OTP_SECRET = "a-private-otp-key-for-these-tests-0123456789abcdef"

ENVIRONMENTS = ["staging", "production"]


@pytest.fixture(autouse=True)
def _no_settings_in_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit values only: remove every variable that names a setting."""
    names = {name.upper() for name in Settings.model_fields}
    for variable in list(os.environ):
        if variable.upper() in names:
            monkeypatch.delenv(variable)


def _settings(**values: Any) -> Settings:
    """Build settings that would start anywhere, except for what a test overrides."""
    values.setdefault("APP_SECRET_KEY", SIGNING_KEY)
    values.setdefault("MFA_ENCRYPTION_KEY", Fernet.generate_key().decode())
    values.setdefault("RATE_LIMIT_TRUST_PROXY_HEADER", False)
    values.setdefault("PATIENT_OTP_SECRET", OTP_SECRET)
    return Settings(_env_file=None, **values)  # type: ignore[call-arg]


class TestOtpSecret:
    def test_there_is_no_default_key_in_code(self) -> None:
        default = Settings.model_fields["PATIENT_OTP_SECRET"].default

        assert isinstance(default, SecretStr)
        assert default.get_secret_value() == ""

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    @pytest.mark.parametrize("value", ["", "   "])
    def test_it_is_required_outside_development(self, environment: str, value: str) -> None:
        """Attack: deploy with no key, and hash every code under a key that is empty."""
        with pytest.raises(ValidationError, match="PATIENT_OTP_SECRET is required"):
            _settings(APP_ENV=environment, PATIENT_OTP_SECRET=value)

    @pytest.mark.parametrize("environment", ["development", *ENVIRONMENTS])
    def test_a_short_key_is_refused_everywhere(self, environment: str) -> None:
        short = "k" * (MIN_PATIENT_OTP_SECRET_LENGTH - 1)

        with pytest.raises(ValidationError, match="PATIENT_OTP_SECRET must be at least 32"):
            _settings(APP_ENV=environment, PATIENT_OTP_SECRET=short)

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    def test_a_private_key_starts(self, environment: str) -> None:
        configured = _settings(APP_ENV=environment)

        assert configured.PATIENT_OTP_SECRET.get_secret_value() == OTP_SECRET
        assert configured.patient_otp_secret_is_ephemeral is False

    def test_development_without_a_key_gets_a_random_one_per_process(self) -> None:
        first = _settings(APP_ENV="development", PATIENT_OTP_SECRET="")
        second = _settings(APP_ENV="development", PATIENT_OTP_SECRET="")

        assert first.patient_otp_secret_is_ephemeral is True
        assert len(first.PATIENT_OTP_SECRET.get_secret_value()) >= MIN_PATIENT_OTP_SECRET_LENGTH
        assert (
            first.PATIENT_OTP_SECRET.get_secret_value()
            != second.PATIENT_OTP_SECRET.get_secret_value()
        )

    def test_the_key_is_never_printed(self) -> None:
        configured = _settings(APP_ENV="production")

        assert OTP_SECRET not in repr(configured)
        assert OTP_SECRET not in str(configured.model_dump())

    def test_a_rejected_key_is_not_quoted_in_the_error(self) -> None:
        short = "short-but-secret"

        with pytest.raises(ValidationError) as raised:
            _settings(APP_ENV="production", PATIENT_OTP_SECRET=short)

        assert short not in str(raised.value)

    def test_it_is_a_different_key_from_the_signing_key(self) -> None:
        """Development generates the two independently."""
        configured = Settings(_env_file=None)  # type: ignore[call-arg]

        assert (
            configured.PATIENT_OTP_SECRET.get_secret_value()
            != configured.APP_SECRET_KEY.get_secret_value()
        )


class TestSmsProvider:
    def test_sms_is_off_by_default(self) -> None:
        assert _settings(APP_ENV="development").SMS_PROVIDER is None

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_provider_is_off(self, value: str) -> None:
        assert _settings(APP_ENV="production", SMS_PROVIDER=value).SMS_PROVIDER is None

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    @pytest.mark.parametrize("value", ["dev", "DEV", " Dev "])
    def test_the_dev_sender_cannot_be_selected_outside_development(
        self, environment: str, value: str
    ) -> None:
        """Attack: ship with the sender that prints sign-in codes."""
        with pytest.raises(ValidationError, match="SMS_PROVIDER must not be 'dev'"):
            _settings(APP_ENV=environment, SMS_PROVIDER=value)

    def test_the_dev_sender_is_allowed_in_development(self) -> None:
        assert _settings(APP_ENV="development", SMS_PROVIDER="DEV").SMS_PROVIDER == "dev"

    @pytest.mark.parametrize("environment", ["development", *ENVIRONMENTS])
    def test_an_unknown_provider_refuses_to_start(self, environment: str) -> None:
        """A typo read as "off" would hide until the first patient could not sign in."""
        with pytest.raises(ValidationError, match="does not exist"):
            _settings(APP_ENV=environment, SMS_PROVIDER="twillio")

    def test_the_api_key_is_a_secret(self) -> None:
        configured = _settings(APP_ENV="development", SMS_API_KEY="provider-credential")

        assert isinstance(configured.SMS_API_KEY, SecretStr)
        assert "provider-credential" not in repr(configured)

    @pytest.mark.parametrize("value", [0, -1, 31])
    def test_the_timeout_is_bounded(self, value: float) -> None:
        with pytest.raises(ValidationError):
            _settings(APP_ENV="development", SMS_TIMEOUT_SECONDS=value)


class TestPatientCookies:
    def test_secure_is_the_default(self) -> None:
        assert _settings(APP_ENV="production").PATIENT_COOKIE_SECURE is True

    @pytest.mark.parametrize("environment", ENVIRONMENTS)
    def test_it_cannot_be_switched_off_outside_development(self, environment: str) -> None:
        """Attack: send the refresh token over plain HTTP in a real deployment."""
        with pytest.raises(ValidationError, match="PATIENT_COOKIE_SECURE must be true"):
            _settings(APP_ENV=environment, PATIENT_COOKIE_SECURE=False)

    def test_it_can_be_switched_off_in_development(self) -> None:
        assert (
            _settings(APP_ENV="development", PATIENT_COOKIE_SECURE=False).PATIENT_COOKIE_SECURE
            is False
        )


class TestAllowedCountryCodes:
    def test_the_default_is_india_only(self) -> None:
        assert _settings().PATIENT_OTP_ALLOWED_COUNTRY_CODES == ["+91"]

    def test_a_json_array_from_the_environment_is_parsed(self) -> None:
        configured = _settings(PATIENT_OTP_ALLOWED_COUNTRY_CODES='["+91", "+971"]')

        assert configured.PATIENT_OTP_ALLOWED_COUNTRY_CODES == ["+91", "+971"]

    @pytest.mark.parametrize("value", ["[]", [], "not json", '"+91"', ["91"], ["+"], ["+0"], ["*"]])
    def test_anything_that_is_not_a_list_of_calling_codes_is_refused(self, value: Any) -> None:
        """Attack: an empty or wildcard list read as "send codes to any country"."""
        with pytest.raises(ValidationError, match="PATIENT_OTP_ALLOWED_COUNTRY_CODES"):
            _settings(PATIENT_OTP_ALLOWED_COUNTRY_CODES=value)


class TestPatientAppOrigins:
    def test_the_default_is_the_local_patient_app(self) -> None:
        assert _settings().PATIENT_APP_ORIGINS == ["http://localhost:5174"]

    def test_origins_are_normalised(self) -> None:
        configured = _settings(PATIENT_APP_ORIGINS='["https://Patients.Example.com/"]')

        assert configured.PATIENT_APP_ORIGINS == ["https://patients.example.com"]

    @pytest.mark.parametrize(
        "value",
        [
            "not json",
            ["*"],
            ["https://*.example.com"],
            ["https://example.com/app"],
            ["https://user:pw@example.com"],
            ["https://example.com?x=1"],
            ["ftp://example.com"],
            ["example.com"],
        ],
    )
    def test_anything_that_is_not_an_exact_origin_is_refused(self, value: Any) -> None:
        """Attack: a wildcard origin makes the cross-site check accept every site."""
        with pytest.raises(ValidationError, match="PATIENT_APP_ORIGINS"):
            _settings(PATIENT_APP_ORIGINS=value)
