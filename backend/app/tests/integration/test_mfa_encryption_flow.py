"""MFA secrets encrypted at rest — real requests, a real database, real TOTP codes.

Covers the whole life of a secret: enrolment, confirmation, sign-in, key
rotation, disabling, the data migration that converts existing plaintext, and
the failure modes (no key, wrong key, corrupted value). The assertions that
matter most read ``users.mfa_secret`` straight from the database.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyotp
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from sqlalchemy import select, text

from app.api.dependencies.db import get_db_session
from app.core.config import settings
from app.core.security import (
    MfaEncryptionNotConfiguredError,
    decrypt_mfa_secret,
    encrypt_mfa_secret,
    generate_totp_secret,
    hash_password,
    is_encrypted_mfa_secret,
    mfa_secret_needs_reencryption,
)
from app.main import create_app
from app.models.user import User
from app.services import auth_service as auth_service_module
from app.services import mfa_key_rotation
from app.services.mfa_key_rotation import reencrypt_mfa_secrets
from app.tests.ai_fakes import RecordingLogger

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from types import ModuleType

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

PASSWORD = "Str0ng!Passw0rd123"  # noqa: S105 — a test credential, not a real one
AUTH = "/api/v1/auth"
UNAVAILABLE = "Multi-factor authentication is temporarily unavailable."


def _key() -> str:
    return Fernet.generate_key().decode()


def _use_keys(
    monkeypatch: pytest.MonkeyPatch, current: str | None, previous: str | None = None
) -> None:
    monkeypatch.setattr(
        settings, "MFA_ENCRYPTION_KEY", SecretStr(current) if current is not None else None
    )
    monkeypatch.setattr(
        settings,
        "MFA_ENCRYPTION_PREVIOUS_KEYS",
        SecretStr(previous) if previous is not None else None,
    )


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """HTTP client sharing the test's rolled-back session."""
    application = create_app()

    async def _override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _override
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://test"
    ) as client:
        yield client
    application.dependency_overrides.clear()


async def _make_user(session: AsyncSession, hospital_id: uuid.UUID, **overrides: Any) -> User:
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"mfa-{uuid.uuid4().hex[:12]}@hospital.example",
        password_hash=hash_password(PASSWORD),
        first_name="Mfa",
        last_name="Tester",
        password_changed_at=datetime.now(UTC),
        **overrides,
    )
    session.add(user)
    await session.flush()
    return user


@pytest_asyncio.fixture
async def user(db_session: AsyncSession, hospital_id: uuid.UUID) -> User:
    """An active user with MFA not yet set up."""
    return await _make_user(db_session, hospital_id)


async def _stored_secret(session: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Read ``users.mfa_secret`` with plain SQL, bypassing the ORM's cached row."""
    result = await session.execute(
        text("SELECT mfa_secret FROM users WHERE id = :id"), {"id": user_id}
    )
    stored: str | None = result.scalar_one()
    return stored


async def _login(api: AsyncClient, user: User) -> dict[str, Any]:
    response = await api.post(f"{AUTH}/login", json={"email": user.email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _bearer(api: AsyncClient, user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await _login(api, user))['access_token']}"}


async def _enroll(api: AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await api.post(f"{AUTH}/mfa/enroll", headers=headers, json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    data: dict[str, Any] = response.json()["data"]
    return data


async def _enroll_and_confirm(api: AsyncClient, user: User) -> str:
    """Take a user through enrolment and confirmation; return the plaintext secret."""
    headers = await _bearer(api, user)
    secret = str((await _enroll(api, headers))["secret"])
    confirmed = await api.post(
        f"{AUTH}/mfa/confirm",
        headers=headers,
        json={"secret": secret, "code": pyotp.TOTP(secret).now()},
    )
    assert confirmed.status_code == 200, confirmed.text
    return secret


# ── Setup, confirmation, sign-in ─────────────────────────────────────────────


class TestSetupFlow:
    async def test_enrolment_stores_the_secret_encrypted(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        enrolled = await _enroll(api, await _bearer(api, user))

        stored = await _stored_secret(db_session, user.id)
        assert stored is not None
        assert stored != enrolled["secret"]
        assert enrolled["secret"] not in stored
        assert is_encrypted_mfa_secret(stored)
        # The backend, holding the key, reads back exactly what the user was given.
        assert decrypt_mfa_secret(stored) == enrolled["secret"]

    async def test_enrolment_alone_does_not_switch_mfa_on(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        await _enroll(api, await _bearer(api, user))

        await db_session.refresh(user)
        assert user.mfa_enabled is False

    async def test_confirmation_stores_the_secret_encrypted_and_enables_mfa(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        secret = await _enroll_and_confirm(api, user)

        stored = await _stored_secret(db_session, user.id)
        assert stored is not None
        assert is_encrypted_mfa_secret(stored)
        assert secret not in stored
        assert decrypt_mfa_secret(stored) == secret
        await db_session.refresh(user)
        assert user.mfa_enabled is True

    async def test_a_wrong_code_at_confirmation_enables_nothing(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        headers = await _bearer(api, user)
        secret = (await _enroll(api, headers))["secret"]
        wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"

        response = await api.post(
            f"{AUTH}/mfa/confirm", headers=headers, json={"secret": secret, "code": wrong}
        )

        assert response.status_code == 401
        await db_session.refresh(user)
        assert user.mfa_enabled is False


class TestLoginFlow:
    async def test_sign_in_with_mfa_decrypts_and_verifies(
        self, api: AsyncClient, user: User
    ) -> None:
        secret = await _enroll_and_confirm(api, user)

        challenge = await _login(api, user)
        assert "mfa_ticket" in challenge
        assert "access_token" not in challenge

        verified = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": challenge["mfa_ticket"], "code": pyotp.TOTP(secret).now()},
        )

        assert verified.status_code == 200, verified.text
        assert verified.json()["data"]["access_token"]

    async def test_a_wrong_code_is_still_just_a_wrong_code(
        self, api: AsyncClient, user: User
    ) -> None:
        secret = await _enroll_and_confirm(api, user)
        challenge = await _login(api, user)
        wrong = "000000" if pyotp.TOTP(secret).now() != "000000" else "111111"

        response = await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": challenge["mfa_ticket"], "code": wrong}
        )

        assert response.status_code == 401
        assert "access_token" not in response.text

    async def test_mfa_can_be_disabled_with_a_valid_code(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        secret = await _enroll_and_confirm(api, user)
        challenge = await _login(api, user)
        verified = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": challenge["mfa_ticket"], "code": pyotp.TOTP(secret).now()},
        )
        headers = {"Authorization": f"Bearer {verified.json()['data']['access_token']}"}

        response = await api.post(
            f"{AUTH}/mfa/disable",
            headers=headers,
            json={"password": PASSWORD, "code": pyotp.TOTP(secret).now()},
        )

        assert response.status_code == 200, response.text
        assert await _stored_secret(db_session, user.id) is None
        await db_session.refresh(user)
        assert user.mfa_enabled is False


# ── Confirmation trusts only the server-side pending secret ──────────────────


class TestConfirmationHardening:
    async def test_a_client_supplied_secret_cannot_replace_the_pending_secret(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """(a) Sending your own secret with a code that matches it gets nowhere."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]
        stored_before = await _stored_secret(db_session, user.id)
        attacker_secret = generate_totp_secret()

        response = await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": attacker_secret, "code": pyotp.TOTP(attacker_secret).now()},
        )

        assert response.status_code == 401, response.text
        stored_after = await _stored_secret(db_session, user.id)
        assert stored_after == stored_before
        assert stored_after is not None
        assert decrypt_mfa_secret(stored_after) == pending
        await db_session.refresh(user)
        assert user.mfa_enabled is False

    async def test_a_code_from_any_other_secret_fails(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """(b) Only a code generated from the pending secret confirms."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]
        other = generate_totp_secret()
        assert other != pending

        # The right secret echoed back, but a code from a different one.
        response = await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": pending, "code": pyotp.TOTP(other).now()},
        )

        assert response.status_code == 401
        await db_session.refresh(user)
        assert user.mfa_enabled is False

    async def test_the_pending_secret_and_a_valid_code_succeed(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """(c) The normal path, exactly as the Hospital frontend sends it."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]
        stored_before = await _stored_secret(db_session, user.id)

        response = await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": pending, "code": pyotp.TOTP(pending).now()},
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(user)
        assert user.mfa_enabled is True
        # Confirmation switches MFA on; it does not rewrite the secret.
        assert await _stored_secret(db_session, user.id) == stored_before

    async def test_the_code_alone_is_enough(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """The request needs only the code; the secret field is optional."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]

        response = await api.post(
            f"{AUTH}/mfa/confirm", headers=headers, json={"code": pyotp.TOTP(pending).now()}
        )

        assert response.status_code == 200, response.text
        await db_session.refresh(user)
        assert user.mfa_enabled is True

    async def test_a_wrong_secret_alongside_the_right_code_is_simply_ignored(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """The client's secret has no effect at all — not even as a veto."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]

        response = await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": generate_totp_secret(), "code": pyotp.TOTP(pending).now()},
        )

        assert response.status_code == 200, response.text
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None
        assert decrypt_mfa_secret(stored) == pending

    async def test_enabled_mfa_cannot_be_replaced_through_confirm(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """(d) With MFA on, confirm rejects — whatever secret and code are sent."""
        active = await _enroll_and_confirm(api, user)
        stored_before = await _stored_secret(db_session, user.id)
        challenge = await _login(api, user)
        verified = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": challenge["mfa_ticket"], "code": pyotp.TOTP(active).now()},
        )
        headers = {"Authorization": f"Bearer {verified.json()['data']['access_token']}"}
        attacker_secret = generate_totp_secret()

        for body in (
            {"secret": attacker_secret, "code": pyotp.TOTP(attacker_secret).now()},
            {"secret": active, "code": pyotp.TOTP(active).now()},
            {"code": pyotp.TOTP(active).now()},
        ):
            response = await api.post(f"{AUTH}/mfa/confirm", headers=headers, json=body)
            assert response.status_code == 400, response.text
            assert "already enabled" in response.json()["message"]

        assert await _stored_secret(db_session, user.id) == stored_before
        await db_session.refresh(user)
        assert user.mfa_enabled is True
        # The original authenticator still signs in; the attacker's does not.
        again = await _login(api, user)
        rejected = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": again["mfa_ticket"], "code": pyotp.TOTP(attacker_secret).now()},
        )
        accepted = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": again["mfa_ticket"], "code": pyotp.TOTP(active).now()},
        )
        assert rejected.status_code == 401
        assert accepted.status_code == 200

    async def test_confirm_without_an_enrolment_in_progress_is_rejected(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """No pending secret: a session alone cannot switch MFA on."""
        headers = await _bearer(api, user)
        attacker_secret = generate_totp_secret()

        response = await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": attacker_secret, "code": pyotp.TOTP(attacker_secret).now()},
        )

        assert response.status_code == 400, response.text
        assert await _stored_secret(db_session, user.id) is None
        await db_session.refresh(user)
        assert user.mfa_enabled is False

    async def test_confirm_requires_authentication(self, api: AsyncClient) -> None:
        response = await api.post(f"{AUTH}/mfa/confirm", json={"code": "123456"})
        assert response.status_code == 401

    async def test_a_pending_secret_is_not_exposed_by_other_endpoints(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """Between enrolment and confirmation the pending secret stays put."""
        headers = await _bearer(api, user)
        pending = (await _enroll(api, headers))["secret"]
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None

        me = await api.get("/api/v1/users/me", headers=headers)
        login = await api.post(f"{AUTH}/login", json={"email": user.email, "password": PASSWORD})
        failed = await api.post(
            f"{AUTH}/mfa/confirm", headers=headers, json={"secret": pending, "code": "000000"}
        )

        for response in (me, login, failed):
            assert pending not in response.text
            assert stored not in response.text
            assert "mfa_secret" not in response.text
        # Enrolment is pending, so a password login still needs no MFA step.
        assert "access_token" in login.text

    async def test_a_failed_confirmation_logs_no_secret(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = RecordingLogger()
        headers = await _bearer(api, user)
        monkeypatch.setattr(auth_service_module, "logger", recorder)
        pending = (await _enroll(api, headers))["secret"]
        attacker_secret = generate_totp_secret()

        await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": attacker_secret, "code": pyotp.TOTP(attacker_secret).now()},
        )
        await api.post(
            f"{AUTH}/mfa/confirm",
            headers=headers,
            json={"secret": pending, "code": pyotp.TOTP(pending).now()},
        )
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None

        logged = json.dumps(recorder.entries, default=str)
        assert "mfa_enabled" in logged
        assert pending not in logged
        assert attacker_secret not in logged
        assert stored not in logged


# ── Nothing leaks ────────────────────────────────────────────────────────────


class TestNoPlaintextLeaves:
    async def test_only_enrolment_ever_returns_the_secret(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """Enrolment hands the secret to the user once. Nothing else returns it —
        and nothing at all returns the stored ciphertext."""
        headers = await _bearer(api, user)
        secret = (await _enroll(api, headers))["secret"]
        code = pyotp.TOTP(secret).now()
        confirm = await api.post(
            f"{AUTH}/mfa/confirm", headers=headers, json={"secret": secret, "code": code}
        )
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None

        login = await api.post(f"{AUTH}/login", json={"email": user.email, "password": PASSWORD})
        verify = await api.post(
            f"{AUTH}/mfa/verify",
            json={
                "mfa_ticket": login.json()["data"]["mfa_ticket"],
                "code": pyotp.TOTP(secret).now(),
            },
        )
        authed = {"Authorization": f"Bearer {verify.json()['data']['access_token']}"}
        me = await api.get("/api/v1/users/me", headers=authed)
        refresh = await api.post(
            f"{AUTH}/refresh", json={"refresh_token": verify.json()["data"]["refresh_token"]}
        )

        for response in (confirm, login, verify, me, refresh):
            assert response.status_code == 200, response.text
            assert secret not in response.text
            assert stored not in response.text
            assert "mfa_secret" not in response.text

    async def test_enrolment_never_returns_the_stored_ciphertext(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        response = await api.post(
            f"{AUTH}/mfa/enroll", headers=await _bearer(api, user), json={"password": PASSWORD}
        )

        stored = await _stored_secret(db_session, user.id)
        assert stored is not None
        assert stored not in response.text

    async def test_no_log_line_carries_the_secret_or_the_ciphertext(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        recorder = RecordingLogger()
        monkeypatch.setattr(auth_service_module, "logger", recorder)

        secret = await _enroll_and_confirm(api, user)
        challenge = await _login(api, user)
        await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": challenge["mfa_ticket"], "code": "000000"}
        )
        await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": challenge["mfa_ticket"], "code": pyotp.TOTP(secret).now()},
        )
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None

        logged = json.dumps(recorder.entries, default=str)
        assert recorder.entries, "the flow should have logged something"
        assert secret not in logged
        assert stored not in logged
        assert settings.mfa_encryption_keys()[0].decode() not in logged

    async def test_the_audit_trail_does_not_carry_the_secret(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        secret = await _enroll_and_confirm(api, user)
        stored = await _stored_secret(db_session, user.id)
        assert stored is not None

        rows = await db_session.execute(
            text(
                "SELECT coalesce(before::text, '') || coalesce(after::text, '') "
                "|| coalesce(context::text, '') FROM audit_logs WHERE target_id = :id"
            ),
            {"id": user.id},
        )
        trail = " ".join(rows.scalars())

        assert secret not in trail
        assert stored not in trail


# ── Failure modes ────────────────────────────────────────────────────────────


class TestMissingKey:
    async def test_enrolment_is_refused_and_nothing_is_stored(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        headers = await _bearer(api, user)
        _use_keys(monkeypatch, None)

        response = await api.post(
            f"{AUTH}/mfa/enroll", headers=headers, json={"password": PASSWORD}
        )

        assert response.status_code == 503
        assert response.json()["message"] == UNAVAILABLE
        assert "provisioning_uri" not in response.text
        # Never the plaintext as a fallback — nothing at all.
        assert await _stored_secret(db_session, user.id) is None

    async def test_sign_in_is_refused_not_waved_through(
        self, api: AsyncClient, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = await _enroll_and_confirm(api, user)
        challenge = await _login(api, user)
        _use_keys(monkeypatch, None)

        response = await api.post(
            f"{AUTH}/mfa/verify",
            json={"mfa_ticket": challenge["mfa_ticket"], "code": pyotp.TOTP(secret).now()},
        )

        assert response.status_code == 503
        assert response.json()["message"] == UNAVAILABLE
        assert "access_token" not in response.text

    async def test_login_never_skips_the_mfa_step(
        self, api: AsyncClient, user: User, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Password alone still yields only a ticket, key or no key."""
        await _enroll_and_confirm(api, user)
        _use_keys(monkeypatch, None)

        challenge = await _login(api, user)

        assert "mfa_ticket" in challenge
        assert "access_token" not in challenge


class TestInvalidCiphertext:
    async def _challenge_with_stored(
        self, api: AsyncClient, db_session: AsyncSession, user: User, stored: str
    ) -> tuple[str, str]:
        secret = await _enroll_and_confirm(api, user)
        await db_session.execute(
            text("UPDATE users SET mfa_secret = :stored WHERE id = :id"),
            {"stored": stored, "id": user.id},
        )
        await db_session.refresh(user)
        return secret, (await _login(api, user))["mfa_ticket"]

    @pytest.mark.parametrize(
        "corrupt",
        ["gAAAAAcorrupted-not-a-real-token", "", "plain"],
        ids=["corrupted-token", "blank-token", "not-a-token"],
    )
    async def test_an_unreadable_secret_fails_closed(
        self, api: AsyncClient, db_session: AsyncSession, user: User, corrupt: str
    ) -> None:
        stored = corrupt or "gAAAAA"
        secret, ticket = await self._challenge_with_stored(api, db_session, user, stored)

        response = await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )

        assert response.status_code == 503
        assert response.json()["message"] == UNAVAILABLE
        assert "access_token" not in response.text
        assert stored not in response.text

    async def test_a_plaintext_secret_in_the_column_does_not_verify(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        """Whoever can write the column cannot plant a secret of their choosing."""
        planted = generate_totp_secret()
        _, ticket = await self._challenge_with_stored(api, db_session, user, planted)

        response = await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": pyotp.TOTP(planted).now()}
        )

        assert response.status_code == 503
        assert "access_token" not in response.text

    async def test_a_secret_under_an_unknown_key_fails_closed(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        secret = await _enroll_and_confirm(api, user)
        ticket = (await _login(api, user))["mfa_ticket"]
        _use_keys(monkeypatch, _key())  # the key was replaced without keeping the old one

        response = await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )

        assert response.status_code == 503
        assert "access_token" not in response.text


# ── Key rotation ─────────────────────────────────────────────────────────────


class TestKeyRotation:
    async def test_signing_in_moves_the_secret_to_the_current_key(
        self,
        api: AsyncClient,
        db_session: AsyncSession,
        user: User,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old, new = _key(), _key()
        _use_keys(monkeypatch, old)
        secret = await _enroll_and_confirm(api, user)
        before = await _stored_secret(db_session, user.id)
        assert before is not None

        _use_keys(monkeypatch, new, previous=old)
        ticket = (await _login(api, user))["mfa_ticket"]
        response = await api.post(
            f"{AUTH}/mfa/verify", json={"mfa_ticket": ticket, "code": pyotp.TOTP(secret).now()}
        )
        assert response.status_code == 200, response.text

        after = await _stored_secret(db_session, user.id)
        assert after is not None
        assert after != before
        # The retired key can now be dropped and the user still signs in.
        _use_keys(monkeypatch, new)
        assert decrypt_mfa_secret(after) == secret

    async def test_the_rotation_command_reencrypts_every_stored_secret(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old, new = _key(), _key()
        _use_keys(monkeypatch, old)
        secrets = {hospital_id: generate_totp_secret(), other_hospital_id: generate_totp_secret()}
        users = {
            hid: await _make_user(
                db_session, hid, mfa_secret=encrypt_mfa_secret(secret), mfa_enabled=True
            )
            for hid, secret in secrets.items()
        }
        deleted = await _make_user(
            db_session,
            hospital_id,
            mfa_secret=encrypt_mfa_secret(generate_totp_secret()),
            deleted_at=datetime.now(UTC),
        )

        _use_keys(monkeypatch, new, previous=old)
        current = await _make_user(
            db_session, hospital_id, mfa_secret=encrypt_mfa_secret(generate_totp_secret())
        )
        broken = await _make_user(db_session, hospital_id, mfa_secret="gAAAAAbroken")
        ours = {u.id for u in (*users.values(), deleted, current, broken)}

        report = await reencrypt_mfa_secrets(db_session)

        # Other tests' rows share the database, so counts are lower bounds.
        assert report.reencrypted >= 3
        assert report.already_current >= 1
        assert report.undecryptable >= 1
        assert report.total == report.reencrypted + report.already_current + report.undecryptable

        _use_keys(monkeypatch, new)  # the retired key is gone
        for hid, account in users.items():
            stored = await _stored_secret(db_session, account.id)
            assert stored is not None
            assert decrypt_mfa_secret(stored) == secrets[hid]
            assert mfa_secret_needs_reencryption(stored) is False
        # A soft-deleted user's secret is still on disk, so it is rotated too.
        stored_deleted = await _stored_secret(db_session, deleted.id)
        assert stored_deleted is not None
        assert decrypt_mfa_secret(stored_deleted)
        # The unreadable row is reported and left exactly as it was.
        assert await _stored_secret(db_session, broken.id) == "gAAAAAbroken"
        assert len(ours) == 5

    async def test_the_rotation_command_logs_no_secret(
        self,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        old, new = _key(), _key()
        _use_keys(monkeypatch, old)
        secret = generate_totp_secret()
        stored = encrypt_mfa_secret(secret)
        await _make_user(db_session, hospital_id, mfa_secret=stored)
        _use_keys(monkeypatch, new, previous=old)
        recorder = RecordingLogger()
        monkeypatch.setattr(mfa_key_rotation, "logger", recorder)

        await reencrypt_mfa_secrets(db_session)

        logged = json.dumps(recorder.entries, default=str)
        assert "mfa_key_rotation_completed" in logged
        assert secret not in logged
        assert stored not in logged
        assert old not in logged
        assert new not in logged

    async def test_the_rotation_command_needs_a_key(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        await _make_user(db_session, hospital_id, mfa_secret=encrypt_mfa_secret("JBSWY3DPEHPK3PXP"))
        _use_keys(monkeypatch, None)

        with pytest.raises(MfaEncryptionNotConfiguredError):
            await reencrypt_mfa_secrets(db_session)


# ── The data migration ───────────────────────────────────────────────────────


def _migration() -> ModuleType:
    path = (
        Path(__file__).resolve().parents[3]
        / "migrations"
        / "versions"
        / "0018_encrypt_mfa_secrets.py"
    )
    spec = importlib.util.spec_from_file_location("migration_0018", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _run(session: AsyncSession, function: Any, keys: list[bytes]) -> int:
    """Run one of the migration's functions on the test's own connection."""
    return await session.run_sync(lambda sync: function(sync.connection(), keys))


async def _plaintext_rows(session: AsyncSession) -> int:
    result = await session.execute(
        text(
            "SELECT count(*) FROM users "
            "WHERE mfa_secret IS NOT NULL AND mfa_secret NOT LIKE 'gAAAAA%'"
        )
    )
    return int(result.scalar_one())


class TestMigration:
    async def test_it_follows_the_last_migration(self) -> None:
        module = _migration()
        assert module.revision == "0018"
        assert module.down_revision == "0017"

    async def test_existing_plaintext_secrets_are_encrypted_and_still_work(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        module = _migration()
        key = Fernet.generate_key()
        secret = generate_totp_secret()
        legacy = await _make_user(db_session, hospital_id, mfa_secret=secret, mfa_enabled=True)
        untouched = await _make_user(db_session, hospital_id)

        converted = await _run(db_session, module.encrypt_plaintext_secrets, [key])

        assert converted >= 1
        assert await _plaintext_rows(db_session) == 0
        stored = await _stored_secret(db_session, legacy.id)
        assert stored is not None
        assert stored != secret
        assert Fernet(key).decrypt(stored.encode()).decode() == secret
        # The user's authenticator keeps working: same secret, same codes.
        assert pyotp.TOTP(Fernet(key).decrypt(stored.encode()).decode()).verify(
            pyotp.TOTP(secret).now()
        )
        await db_session.refresh(legacy)
        assert legacy.mfa_enabled is True
        assert await _stored_secret(db_session, untouched.id) is None

    async def test_it_is_idempotent(self, db_session: AsyncSession, hospital_id: uuid.UUID) -> None:
        module = _migration()
        key = Fernet.generate_key()
        legacy = await _make_user(db_session, hospital_id, mfa_secret=generate_totp_secret())

        await _run(db_session, module.encrypt_plaintext_secrets, [key])
        first = await _stored_secret(db_session, legacy.id)
        again = await _run(db_session, module.encrypt_plaintext_secrets, [key])

        assert again == 0
        assert await _stored_secret(db_session, legacy.id) == first

    async def test_it_refuses_to_run_without_a_key_when_plaintext_exists(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        module = _migration()
        secret = generate_totp_secret()
        legacy = await _make_user(db_session, hospital_id, mfa_secret=secret, mfa_enabled=True)

        with pytest.raises(RuntimeError, match="MFA_ENCRYPTION_KEY is not set") as raised:
            await _run(db_session, module.encrypt_plaintext_secrets, [])

        # It names the count, never a secret, and changed nothing.
        assert secret not in str(raised.value)
        assert await _stored_secret(db_session, legacy.id) == secret
        await db_session.refresh(legacy)
        assert legacy.mfa_enabled is True

    async def test_without_plaintext_it_needs_no_key(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """A fresh database, or one where nobody enrolled, upgrades with no key."""
        module = _migration()
        await _run(db_session, module.encrypt_plaintext_secrets, [Fernet.generate_key()])
        assert await _plaintext_rows(db_session) == 0

        assert await _run(db_session, module.encrypt_plaintext_secrets, []) == 0

    async def test_downgrade_refuses_and_never_creates_plaintext(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """A downgrade with encrypted secrets stops, and writes nothing."""
        module = _migration()
        key = Fernet.generate_key()
        secret = generate_totp_secret()
        legacy = await _make_user(db_session, hospital_id, mfa_secret=secret, mfa_enabled=True)
        await _run(db_session, module.encrypt_plaintext_secrets, [key])
        encrypted = await _stored_secret(db_session, legacy.id)
        assert encrypted is not None
        assert await _plaintext_rows(db_session) == 0

        with pytest.raises(RuntimeError, match="Refusing to downgrade") as raised:
            await db_session.run_sync(
                lambda sync: module.refuse_downgrade_if_encrypted(sync.connection())
            )

        # No plaintext was created, the ciphertext is byte-for-byte unchanged,
        # and MFA is still switched on for the user.
        assert await _plaintext_rows(db_session) == 0
        assert await _stored_secret(db_session, legacy.id) == encrypted
        assert await _stored_secret(db_session, legacy.id) != secret
        await db_session.refresh(legacy)
        assert legacy.mfa_enabled is True
        # The error explains the way back and carries neither secret nor ciphertext.
        message = str(raised.value)
        assert "Roll forward" in message
        assert "restore a backup" in message
        assert secret not in message
        assert encrypted not in message

    async def test_downgrade_refuses_even_with_the_key_available(
        self, db_session: AsyncSession, hospital_id: uuid.UUID, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Having the key changes nothing: the downgrade does not decrypt."""
        module = _migration()
        key = _key()
        _use_keys(monkeypatch, key)
        account = await _make_user(
            db_session, hospital_id, mfa_secret=encrypt_mfa_secret(generate_totp_secret())
        )
        before = await _stored_secret(db_session, account.id)

        with pytest.raises(RuntimeError, match="Refusing to downgrade"):
            await db_session.run_sync(
                lambda sync: module.refuse_downgrade_if_encrypted(sync.connection())
            )

        assert await _stored_secret(db_session, account.id) == before
        assert await _plaintext_rows(db_session) == 0

    async def test_downgrade_is_a_no_op_when_no_secret_is_stored(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        """A database with no MFA secrets has nothing to undo, so it may go back."""
        module = _migration()
        await _make_user(db_session, hospital_id)
        encrypted = await db_session.execute(
            text("SELECT count(*) FROM users WHERE mfa_secret LIKE 'gAAAAA%'")
        )
        assert encrypted.scalar_one() == 0

        await db_session.run_sync(
            lambda sync: module.refuse_downgrade_if_encrypted(sync.connection())
        )

        assert await _plaintext_rows(db_session) == 0

    async def test_the_migration_has_no_code_path_that_decrypts(self) -> None:
        """Structural: nothing in the migration can turn ciphertext into plaintext."""
        source = Path(_migration().__file__ or "").read_text()
        code = source.split('"""', 2)[2]  # everything after the module docstring

        assert ".decrypt(" not in code
        assert "MultiFernet" not in code
        assert not hasattr(_migration(), "decrypt_encrypted_secrets")

    async def test_the_migration_reads_its_keys_from_configuration(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        module = _migration()
        current, retired = _key(), _key()
        _use_keys(monkeypatch, current, previous=retired)

        assert module._configured_keys() == [current.encode(), retired.encode()]  # noqa: SLF001


class TestNoPlaintextRemains:
    async def test_every_stored_secret_written_by_the_application_is_encrypted(
        self, api: AsyncClient, db_session: AsyncSession, user: User
    ) -> None:
        await _enroll_and_confirm(api, user)

        rows = await db_session.execute(
            select(User.mfa_secret).where(User.id == user.id, User.mfa_secret.is_not(None))
        )
        stored = [value for value in rows.scalars() if value is not None]
        assert stored
        assert all(is_encrypted_mfa_secret(value) for value in stored)
