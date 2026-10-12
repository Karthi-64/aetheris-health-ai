"""A patient bearer token in the middleware: identified as a patient, never as staff.

``app.middleware.auth`` resolves identity for the rate limiter and the request
log. A Patient App token must be billed to the patient account — and must
never be mistaken for a staff user, least of all for one with no hospital,
which is what a platform administrator looks like.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.datastructures import Headers

from app.core.config import settings
from app.core.security import create_access_token, create_patient_access_token
from app.middleware.auth import AuthMiddleware
from app.middleware.rate_limit import RateLimitMiddleware


async def _resolved(token: str) -> Any:
    """Run the auth middleware over a request carrying *token*; return its state."""
    request = SimpleNamespace(
        headers={"Authorization": f"Bearer {token}"},
        state=SimpleNamespace(),
        url=SimpleNamespace(path="/api/v1/patient/me"),
    )

    async def _call_next(_: Any) -> Any:
        return object()

    middleware = AuthMiddleware(app=lambda *a, **k: None)  # type: ignore[arg-type]
    await middleware.dispatch(request, _call_next)  # type: ignore[arg-type]
    return request.state


def _limited(state: Any, path: str = "/api/v1/patient/me") -> list[tuple[str, int]]:
    request = SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=state,
        client=SimpleNamespace(host="203.0.113.7"),
        headers=Headers({}),
    )
    return RateLimitMiddleware._applicable_limits(request)  # type: ignore[arg-type]  # noqa: SLF001


class TestPatientTokenInTheAuthMiddleware:
    async def test_it_sets_the_patient_account_and_no_staff_identity(self) -> None:
        """Attack: a patient token taken for a staff user with no hospital (a Super Admin)."""
        account_id = uuid.uuid4()

        state = await _resolved(create_patient_access_token(account_id))

        assert state.patient_account_id == account_id
        assert state.user_id is None
        assert state.hospital_id is None

    async def test_a_staff_token_sets_no_patient_account(self) -> None:
        user_id, hospital_id = uuid.uuid4(), uuid.uuid4()

        state = await _resolved(create_access_token(user_id, hospital_id))

        assert state.user_id == user_id
        assert state.hospital_id == hospital_id
        assert state.patient_account_id is None

    async def test_a_garbage_token_sets_neither(self) -> None:
        state = await _resolved("not.a.jwt")

        assert state.user_id is None
        assert state.patient_account_id is None

    async def test_no_token_still_defines_the_attribute(self) -> None:
        request = SimpleNamespace(
            headers={}, state=SimpleNamespace(), url=SimpleNamespace(path="/api/v1/patient/me")
        )

        async def _call_next(_: Any) -> Any:
            return object()

        await AuthMiddleware(app=lambda *a, **k: None).dispatch(request, _call_next)  # type: ignore[arg-type]

        assert request.state.patient_account_id is None


class TestPatientTokenInTheRateLimiter:
    async def test_a_patient_is_billed_to_its_own_account(self) -> None:
        account_id = uuid.uuid4()
        state = await _resolved(create_patient_access_token(account_id))

        assert _limited(state) == [(f"patient:{account_id}", settings.RATE_LIMIT_USER_PER_MIN)]

    async def test_a_patient_is_never_billed_as_a_user_or_a_hospital(self) -> None:
        state = await _resolved(create_patient_access_token(uuid.uuid4()))

        keys = [key for key, _ in _limited(state)]

        assert not any(key.startswith(("user:", "hospital:", "ai:", "ip:")) for key in keys)

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/patient/me",
            "/api/v1/patient/auth/logout-all",
            "/api/v1/patient/auth/refresh",
            "/api/v1/patient/hospitals/city-clinic/link",
            "/api/v1/patient/hospitals/city-clinic/register",
            # Not the public code endpoints, only paths that resemble them.
            "/api/v1/patient/auth/otp",
            "/api/v1/patient/auth/otp-history",
        ],
    )
    async def test_the_account_tier_covers_the_patient_namespace(self, path: str) -> None:
        account_id = uuid.uuid4()
        state = await _resolved(create_patient_access_token(account_id))

        assert _limited(state, path) == [
            (f"patient:{account_id}", settings.RATE_LIMIT_USER_PER_MIN)
        ]

    @pytest.mark.parametrize(
        "path",
        [
            # Staff sign-in and recovery: guessing endpoints.
            "/api/v1/auth/login",
            "/api/v1/auth/forgot-password",
            "/api/v1/auth/refresh",
            # Staff data, including the namesake that is *not* the Patient App.
            "/api/v1/patients",
            "/api/v1/patients/",
            "/api/v1/users",
            # The Patient App's public code endpoints: anonymous by design.
            "/api/v1/patient/auth/otp/request",
            "/api/v1/patient/auth/otp/verify",
            "/api/v1/patient/auth/otp/",
            "/api/v1/patient/auth/otp/anything/else",
            # Not the namespace: no trailing slash, another case, a prefix of it, the root.
            "/api/v1/patient",
            "/API/V1/PATIENT/me",
            "/api/v1/patient-portal/me",
            "/v1/patient/me",
            "/healthz",
            "/",
        ],
    )
    async def test_a_patient_token_changes_nothing_anywhere_else(self, path: str) -> None:
        """Attack: attach a self-service patient token to buy the per-account allowance.

        Anybody with a phone can obtain one. Off the Patient App's own
        authenticated endpoints the request is counted exactly as if it
        carried no token at all: against its source, at the anonymous limit.
        """
        state = await _resolved(create_patient_access_token(uuid.uuid4()))
        anonymous = SimpleNamespace(user_id=None, hospital_id=None, patient_account_id=None)

        assert _limited(state, path) == _limited(anonymous, path)
        assert _limited(state, path) == [("ip:203.0.113.7", settings.RATE_LIMIT_ANON_PER_MIN)]

    async def test_many_patient_tokens_from_one_address_share_that_addresses_allowance(
        self,
    ) -> None:
        """Attack: rotate through accounts to multiply the allowance on the code endpoints."""
        keys: set[str] = set()
        for _ in range(5):
            state = await _resolved(create_patient_access_token(uuid.uuid4()))
            keys.update(key for key, _ in _limited(state, "/api/v1/patient/auth/otp/request"))
            keys.update(key for key, _ in _limited(state, "/api/v1/auth/login"))

        assert keys == {"ip:203.0.113.7"}

    async def test_a_patient_token_on_an_ai_path_is_held_to_the_anonymous_ai_limit(self) -> None:
        state = await _resolved(create_patient_access_token(uuid.uuid4()))

        assert _limited(state, "/api/v1/ai/chat") == [
            ("ip:203.0.113.7", settings.RATE_LIMIT_AI_PER_MIN)
        ]

    def test_a_request_with_no_patient_attribute_is_still_anonymous(self) -> None:
        """State built before this change (no ``patient_account_id``) keeps working."""
        state = SimpleNamespace(user_id=None, hospital_id=None)

        assert _limited(state) == [("ip:203.0.113.7", settings.RATE_LIMIT_ANON_PER_MIN)]

    def test_a_staff_user_is_billed_exactly_as_before(self) -> None:
        user_id, hospital_id = uuid.uuid4(), uuid.uuid4()
        state = SimpleNamespace(user_id=user_id, hospital_id=hospital_id, patient_account_id=None)

        assert _limited(state) == [
            (f"user:{user_id}", settings.RATE_LIMIT_USER_PER_MIN),
            (f"hospital:{hospital_id}", settings.RATE_LIMIT_HOSPITAL_PER_MIN),
        ]
