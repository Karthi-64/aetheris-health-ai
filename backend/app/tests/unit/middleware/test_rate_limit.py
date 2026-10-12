"""Unit tests for :mod:`app.middleware.rate_limit`.

These cover the tier selection rules in ``docs/06-API_STANDARDS.md`` §15 and the
failure behaviour around Redis. The property under test throughout is *which
budget a request is billed against* — the defect these tests exist to prevent
was authenticated traffic silently falling through to the anonymous per-IP
tier, which behind a proxy meant an entire hospital sharing 60 requests per
minute while ``RATE_LIMIT_USER_PER_MIN`` sat unused.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from starlette.datastructures import Headers

from app.core.config import settings
from app.middleware import rate_limit
from app.middleware.rate_limit import (
    InMemoryRateLimiter,
    RateLimitMiddleware,
    _client_ip,
    reset_limiter_state,
)

# Captured at import, before the suite-wide ``reset_rate_limiters`` fixture
# replaces it with a stub that forces the circuit open. The Redis tests below
# restore this so they exercise the real short-circuit logic.
_REAL_CIRCUIT_CHECK = rate_limit._redis_is_circuit_open


def _request(
    *,
    path: str = "/api/v1/patients",
    user_id: uuid.UUID | None = None,
    hospital_id: uuid.UUID | None = None,
    client_host: str | None = "203.0.113.7",
    headers: dict[str, str] | None = None,
) -> Any:
    """Build a stand-in request exposing only what the middleware reads."""
    return SimpleNamespace(
        url=SimpleNamespace(path=path),
        state=SimpleNamespace(user_id=user_id, hospital_id=hospital_id),
        client=SimpleNamespace(host=client_host) if client_host else None,
        headers=Headers(headers or {}),
    )


@pytest.fixture(autouse=True)
def _clean_limiter_state() -> None:
    """Reset the module-level counters between cases."""
    reset_limiter_state()


class TestTierSelection:
    """Which ``(key, limit)`` pairs a request is checked against."""

    def test_anonymous_request_uses_ip_and_anon_limit(self) -> None:
        limits = RateLimitMiddleware._applicable_limits(_request())

        assert limits == [("ip:203.0.113.7", settings.RATE_LIMIT_ANON_PER_MIN)]

    def test_authenticated_request_uses_user_limit_not_ip(self) -> None:
        """The regression guard: an identified caller must never be billed by IP."""
        user_id = uuid.uuid4()

        limits = RateLimitMiddleware._applicable_limits(_request(user_id=user_id))

        assert (f"user:{user_id}", settings.RATE_LIMIT_USER_PER_MIN) in limits
        assert not any(key.startswith("ip:") for key, _ in limits)

    def test_authenticated_request_also_checks_hospital_ceiling(self) -> None:
        """§15 caps a tenant as well as a user, so both budgets apply."""
        user_id, hospital_id = uuid.uuid4(), uuid.uuid4()

        limits = RateLimitMiddleware._applicable_limits(
            _request(user_id=user_id, hospital_id=hospital_id)
        )

        assert limits == [
            (f"user:{user_id}", settings.RATE_LIMIT_USER_PER_MIN),
            (f"hospital:{hospital_id}", settings.RATE_LIMIT_HOSPITAL_PER_MIN),
        ]

    def test_token_without_hospital_claim_still_gets_user_limit(self) -> None:
        user_id = uuid.uuid4()

        limits = RateLimitMiddleware._applicable_limits(_request(user_id=user_id, hospital_id=None))

        assert limits == [(f"user:{user_id}", settings.RATE_LIMIT_USER_PER_MIN)]

    def test_ai_path_uses_the_lower_ai_budget(self) -> None:
        """AI calls cost money per request, so they get their own ceiling."""
        user_id = uuid.uuid4()

        limits = RateLimitMiddleware._applicable_limits(
            _request(path="/api/v1/appointments/recommend-slot", user_id=user_id)
        )

        assert limits[0] == (f"ai:{user_id}", settings.RATE_LIMIT_AI_PER_MIN)

    def test_anonymous_ai_path_uses_ai_budget_on_the_ip_key(self) -> None:
        limits = RateLimitMiddleware._applicable_limits(
            _request(path="/api/v1/appointments/recommend-slot")
        )

        assert limits == [("ip:203.0.113.7", settings.RATE_LIMIT_AI_PER_MIN)]


class TestClientIp:
    """Proxy-header trust. Getting this wrong fails open in both directions."""

    #: A socket peer inside the default ``RATE_LIMIT_TRUSTED_PROXY_CIDRS``: our proxy.
    PROXY = "10.0.0.5"

    @pytest.fixture(autouse=True)
    def _shipped_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Start every case from the shipped proxy settings, whatever the shell exports."""
        from app.core.config import Settings

        for name in (
            "RATE_LIMIT_TRUST_PROXY_HEADER",
            "RATE_LIMIT_TRUSTED_PROXY_HOPS",
            "RATE_LIMIT_TRUSTED_PROXY_CIDRS",
        ):
            default = Settings.model_fields[name].default
            monkeypatch.setattr(
                settings, name, list(default) if isinstance(default, list) else default
            )

    def _anonymous_key(self, request: Any) -> str:
        """The budget an anonymous request is billed against."""
        [(key, _limit)] = RateLimitMiddleware._applicable_limits(request)
        return key

    def test_ignores_forwarded_header_by_default(self) -> None:
        """Untrusted X-Forwarded-For would let any client mint a fresh bucket."""
        request = _request(headers={"X-Forwarded-For": "198.51.100.1"})

        assert _client_ip(request) == "203.0.113.7"

    def test_a_new_forged_address_per_request_is_still_one_budget(self) -> None:
        """The attack on the anonymous tier: a fresh claimed address for every request."""
        keys = {
            self._anonymous_key(_request(headers={"X-Forwarded-For": f"198.51.100.{i}"}))
            for i in range(1, 100)
        }

        assert keys == {"ip:203.0.113.7"}

    def test_a_client_connecting_directly_is_not_believed_even_when_trust_is_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attack: go around the proxy and bring your own X-Forwarded-For.

        Trust in the header is trust in *our proxy*. A connection that does not
        come from one of its networks cannot name its own address.
        """
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        keys = {
            self._anonymous_key(_request(headers={"X-Forwarded-For": f"198.51.100.{i}"}))
            for i in range(1, 100)
        }

        assert keys == {"ip:203.0.113.7"}

    def test_uses_forwarded_header_when_trusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        request = _request(client_host=self.PROXY, headers={"X-Forwarded-For": "198.51.100.1"})

        assert _client_ip(request) == "198.51.100.1"

    def test_callers_behind_our_proxy_get_a_budget_each(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The other way to fail: everyone behind the proxy sharing its address."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        first = _request(client_host=self.PROXY, headers={"X-Forwarded-For": "198.51.100.1"})
        second = _request(client_host=self.PROXY, headers={"X-Forwarded-For": "198.51.100.2"})

        assert self._anonymous_key(first) == "ip:198.51.100.1"
        assert self._anonymous_key(second) == "ip:198.51.100.2"

    def test_takes_the_entry_our_own_proxy_appended(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Everything left of the trusted hops is the client's own claim."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        request = _request(
            client_host=self.PROXY,
            headers={"X-Forwarded-For": "198.51.100.1, 10.0.0.1, 192.0.2.44"},
        )

        assert _client_ip(request) == "192.0.2.44"

    def test_a_forged_leading_entry_changes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The attack: send your own X-Forwarded-For and let the proxy append to it."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        honest = _request(client_host=self.PROXY, headers={"X-Forwarded-For": "192.0.2.44"})
        forged = _request(
            client_host=self.PROXY, headers={"X-Forwarded-For": "10.9.9.9, 192.0.2.44"}
        )

        assert _client_ip(forged) == _client_ip(honest) == "192.0.2.44"
        assert self._anonymous_key(forged) == self._anonymous_key(honest) == "ip:192.0.2.44"

    def test_two_proxies_count_two_entries_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUSTED_PROXY_HOPS", 2)
        request = _request(
            client_host=self.PROXY,
            headers={"X-Forwarded-For": "198.51.100.1, 192.0.2.44, 10.0.0.9"},
        )

        assert _client_ip(request) == "192.0.2.44"

    def test_a_garbage_entry_is_billed_to_the_peer_never_used_as_a_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The attack: put arbitrary text where the address goes, to mint arbitrary keys."""
        monkeypatch.setattr(settings, "RATE_LIMIT_TRUST_PROXY_HEADER", True)
        keys = {
            self._anonymous_key(
                _request(client_host=self.PROXY, headers={"X-Forwarded-For": f"junk-{i}"})
            )
            for i in range(50)
        }

        assert keys == {f"ip:{self.PROXY}"}

    def test_an_ipv4_mapped_peer_is_the_same_budget_as_its_ipv4_address(self) -> None:
        """One machine must not get two budgets by switching socket family."""
        mapped = _request(client_host="::ffff:203.0.113.7")

        assert self._anonymous_key(mapped) == self._anonymous_key(_request()) == "ip:203.0.113.7"

    def test_falls_back_when_peer_is_unknown(self) -> None:
        assert _client_ip(_request(client_host=None)) == "unknown"

    def test_callers_with_no_address_at_all_share_one_budget(self) -> None:
        """Hiding where a request came from earns no budget of its own.

        A request with no socket peer has no address; all such requests are
        billed to the single ``ip:unknown`` key, whatever the header claims.
        """
        keys = {
            self._anonymous_key(
                _request(client_host=None, headers={"X-Forwarded-For": f"198.51.100.{i}"})
            )
            for i in range(1, 50)
        }
        keys.add(self._anonymous_key(_request(client_host=None)))

        assert keys == {"ip:unknown"}

    @pytest.mark.parametrize("environment", ["development", "staging", "production"])
    def test_an_insider_cannot_move_itself_into_the_shared_unknown_budget(
        self, monkeypatch: pytest.MonkeyPatch, environment: str
    ) -> None:
        """Attack: from a private address, add ``X-Forwarded-For`` to be billed as "unknown".

        With trust off there is no guessing at an undeclared proxy: a caller
        on the internal network is billed by its own address in every
        environment, whatever header it sends. It can neither spend the
        budget every address-less request shares nor escape its own.
        """
        from app.core.config import AppEnv

        monkeypatch.setattr(settings, "APP_ENV", AppEnv(environment))
        keys = {
            self._anonymous_key(
                _request(client_host=self.PROXY, headers={"X-Forwarded-For": f"198.51.100.{i}"})
            )
            for i in range(1, 50)
        }

        assert keys == {f"ip:{self.PROXY}"}

    def test_the_resolver_is_the_one_the_authentication_throttle_uses(self) -> None:
        """Two resolvers would be two answers to "who is this?" — one of them forgeable."""
        from app.api.v1 import auth as auth_routes
        from app.core import client_ip as shared

        assert vars(rate_limit)["client_ip"] is shared.client_ip
        assert vars(auth_routes)["client_ip"] is shared.client_ip


class TestInMemoryRateLimiter:
    """The fallback counter used when Redis is unreachable."""

    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        limiter = InMemoryRateLimiter()

        results = [limiter.check("k", limit=3)[0] for _ in range(4)]

        assert results == [True, True, True, False]

    def test_remaining_counts_down(self) -> None:
        limiter = InMemoryRateLimiter()

        assert limiter.check("k", limit=3)[1] == 2
        assert limiter.check("k", limit=3)[1] == 1

    def test_keys_are_independent(self) -> None:
        limiter = InMemoryRateLimiter()
        limiter.check("a", limit=1)

        allowed, _, _ = limiter.check("b", limit=1)

        assert allowed is True

    def test_blocked_request_is_not_counted_again(self) -> None:
        """A rejected request must not extend the window it was rejected by."""
        limiter = InMemoryRateLimiter()
        limiter.check("k", limit=1)

        limiter.check("k", limit=1)
        _, remaining, _ = limiter.check("k", limit=1)

        assert remaining == 0


class TestRedisFallback:
    """Redis is optional infrastructure; an outage must not take the API down."""

    @pytest.fixture(autouse=True)
    def _use_real_circuit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Undo the suite-wide stub so Redis is actually attempted here."""
        monkeypatch.setattr(rate_limit, "_redis_is_circuit_open", _REAL_CIRCUIT_CHECK)
        reset_limiter_state()

    @pytest.mark.asyncio
    async def test_falls_back_to_memory_when_redis_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def _boom(key: str, limit: int) -> tuple[bool, int, int]:
            raise RedisConnectionError("no route to host")

        monkeypatch.setattr(rate_limit._redis_limiter, "check", _boom)

        allowed, remaining, _ = await RateLimitMiddleware._check("user:x", limit=5)

        assert allowed is True
        assert remaining == 4

    @pytest.mark.asyncio
    async def test_failure_opens_the_circuit_so_redis_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retrying a dead Redis per request would add its timeout to every call."""
        calls = 0

        async def _boom(key: str, limit: int) -> tuple[bool, int, int]:
            nonlocal calls
            calls += 1
            raise RedisConnectionError("no route to host")

        monkeypatch.setattr(rate_limit._redis_limiter, "check", _boom)

        await RateLimitMiddleware._check("user:x", limit=5)
        await RateLimitMiddleware._check("user:x", limit=5)
        await RateLimitMiddleware._check("user:x", limit=5)

        assert calls == 1

    @pytest.mark.asyncio
    async def test_uses_redis_result_when_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def _ok(key: str, limit: int) -> tuple[bool, int, int]:
            return False, 0, 1_800_000_000

        monkeypatch.setattr(rate_limit._redis_limiter, "check", _ok)

        allowed, remaining, reset_at = await RateLimitMiddleware._check("user:x", limit=5)

        assert (allowed, remaining, reset_at) == (False, 0, 1_800_000_000)


class TestHeaderBudget:
    """Advertised headers must describe the tightest applicable budget."""

    def test_keeps_the_smaller_remaining(self) -> None:
        current = (300, 250, 100)

        assert RateLimitMiddleware._tightest(current, 1000, 10, 200) == (1000, 10, 200)

    def test_keeps_existing_when_it_is_tighter(self) -> None:
        current = (300, 5, 100)

        assert RateLimitMiddleware._tightest(current, 1000, 900, 200) == current

    def test_first_budget_wins_when_none_recorded(self) -> None:
        assert RateLimitMiddleware._tightest(None, 300, 299, 100) == (300, 299, 100)
