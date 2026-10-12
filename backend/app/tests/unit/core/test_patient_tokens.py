"""Patient access tokens: a different kind of token from a staff token.

``docs/modules/15-patient-app.md`` §5.6. The two kinds must be refused
everywhere the other is expected. These tests attack that boundary directly at
the signing and verifying functions; the HTTP-level proof is in the API tests.

No database.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt as pyjwt
import pytest

from app.core.config import settings
from app.core.security import (
    PATIENT_ACCESS_TTL_SECONDS,
    PATIENT_TOKEN_AUDIENCE,
    PATIENT_TOKEN_TYPE,
    create_access_token,
    create_mfa_ticket,
    create_patient_access_token,
    verify_access_token,
    verify_patient_access_token,
)


def _forge(**claims: Any) -> str:
    """Sign arbitrary claims with the real key — what a bug elsewhere might mint."""
    now = datetime.now(UTC)
    payload: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "iss": settings.JWT_ISSUER,
        "iat": now,
        "exp": now + timedelta(minutes=5),
    }
    payload.update(claims)
    payload = {key: value for key, value in payload.items() if value is not None}
    return pyjwt.encode(payload, settings.APP_SECRET_KEY.get_secret_value(), algorithm="HS256")


class TestPatientToken:
    def test_it_names_the_account_and_nothing_else(self) -> None:
        account_id = uuid.uuid4()

        payload = verify_patient_access_token(create_patient_access_token(account_id))

        assert payload["sub"] == str(account_id)
        assert payload["aud"] == PATIENT_TOKEN_AUDIENCE == "atheris-patient"
        assert payload["type"] == PATIENT_TOKEN_TYPE == "patient_access"
        assert payload["iss"] == settings.JWT_ISSUER
        assert set(payload) == {"sub", "iss", "aud", "iat", "exp", "type"}

    def test_it_carries_no_hospital_role_or_permission(self) -> None:
        """A principal with no hospital is a platform administrator in staff auth."""
        token = create_patient_access_token(uuid.uuid4())

        payload = pyjwt.decode(token, options={"verify_signature": False})

        assert "hospital_id" not in payload
        assert "roles" not in payload
        assert "permissions" not in payload

    def test_it_lives_for_fifteen_minutes(self) -> None:
        payload = verify_patient_access_token(create_patient_access_token(uuid.uuid4()))

        assert PATIENT_ACCESS_TTL_SECONDS == 900
        assert payload["exp"] - payload["iat"] == 900

    def test_configuration_cannot_lengthen_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "JWT_ACCESS_TTL_SECONDS", 86400)

        payload = verify_patient_access_token(create_patient_access_token(uuid.uuid4()))

        assert payload["exp"] - payload["iat"] == 900

    def test_an_expired_token_is_refused(self) -> None:
        past = datetime.now(UTC) - timedelta(hours=1)
        token = _forge(aud=PATIENT_TOKEN_AUDIENCE, type=PATIENT_TOKEN_TYPE, iat=past, exp=past)

        with pytest.raises(pyjwt.ExpiredSignatureError):
            verify_patient_access_token(token)

    def test_a_token_signed_with_another_key_is_refused(self) -> None:
        now = datetime.now(UTC)
        token = pyjwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "iss": settings.JWT_ISSUER,
                "aud": PATIENT_TOKEN_AUDIENCE,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "type": PATIENT_TOKEN_TYPE,
            },
            "an-attacker-chosen-key-of-sufficient-length-000000",
            algorithm="HS256",
        )

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(token)


class TestTheTwoKindsRefuseEachOther:
    def test_the_staff_verifier_refuses_a_patient_token(self) -> None:
        """Attack: present a patient token to a staff endpoint."""
        token = create_patient_access_token(uuid.uuid4())

        with pytest.raises(pyjwt.InvalidAudienceError):
            verify_access_token(token)

    def test_the_patient_verifier_refuses_a_staff_token(self) -> None:
        """Attack: present a staff token to a patient endpoint."""
        token = create_access_token(uuid.uuid4(), uuid.uuid4(), roles=["Doctor"])

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(token)

    def test_the_patient_verifier_refuses_a_platform_administrators_token(self) -> None:
        token = create_access_token(uuid.uuid4(), None)

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(token)

    def test_the_patient_verifier_refuses_an_mfa_ticket(self) -> None:
        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(create_mfa_ticket(uuid.uuid4()))

    def test_the_audience_alone_is_not_enough(self) -> None:
        """Attack: a staff-typed token that somehow carries the patient audience."""
        token = _forge(aud=PATIENT_TOKEN_AUDIENCE, type="access", hospital_id=None)

        with pytest.raises(pyjwt.InvalidTokenError, match="Not a patient access token"):
            verify_patient_access_token(token)

    def test_the_type_alone_is_not_enough(self) -> None:
        """Attack: a patient-typed token with no audience, or somebody else's."""
        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(_forge(type=PATIENT_TOKEN_TYPE))
        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(_forge(type=PATIENT_TOKEN_TYPE, aud="atheris-staff"))

    @pytest.mark.parametrize("missing", ["sub", "iss", "iat", "exp", "type"])
    def test_every_required_claim_is_required(self, missing: str) -> None:
        claims: dict[str, Any] = {"aud": PATIENT_TOKEN_AUDIENCE, "type": PATIENT_TOKEN_TYPE}
        claims[missing] = None

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(_forge(**claims))

    def test_another_issuers_token_is_refused(self) -> None:
        token = _forge(aud=PATIENT_TOKEN_AUDIENCE, type=PATIENT_TOKEN_TYPE, iss="someone-else")

        with pytest.raises(pyjwt.InvalidTokenError):
            verify_patient_access_token(token)
