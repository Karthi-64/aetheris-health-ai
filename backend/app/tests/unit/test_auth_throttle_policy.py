"""The authentication throttle's arithmetic, attacked without a database.

``app/services/auth_throttle.py`` replaced the account lockout. Everything it
decides comes down to four pure functions over one row (``_is_open``,
``_charge``, ``_refund``, ``_clear``), a key function (``bucket``) and a table
of constants (``POLICIES``). These tests drive those directly with stand-in
rows, and :class:`AuthThrottle` itself over an in-memory stand-in for its
repository, so every number and every edge is pinned exactly:

* an attacker gets the free attempts and then waits, twice as long each time;
* being refused costs nothing and extends nothing, so a closed bucket cannot
  be hammered into staying closed (the old lockout's flaw);
* a correct credential takes back what it was charged and not one unit more,
  so a success can never be turned into a fresh allowance for someone else;
* no setting can weaken any of it.
"""

from __future__ import annotations

import dataclasses
import inspect
import itertools
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock

import pytest

from app.core.config import Settings, settings
from app.services import auth_throttle as module
from app.services.auth_throttle import (
    POLICIES,
    Admission,
    AuthThrottle,
    Backoff,
    Bucket,
    BucketKind,
    Budget,
    bucket,
)

if TYPE_CHECKING:
    from app.models.auth_throttle import AuthThrottleBucket

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)

BACKOFF_KINDS = sorted(k for k, p in POLICIES.items() if isinstance(p, Backoff))
BUDGET_KINDS = sorted(k for k, p in POLICIES.items() if isinstance(p, Budget))


@pytest.fixture(autouse=True)
def _never_sweep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the occasional purge (one admission in 32, at random) not happen.

    It commits once more when it runs, and several tests count commits. The
    tests about the purge itself switch it on.
    """
    monkeypatch.setattr(module, "secrets", SimpleNamespace(randbelow=lambda _n: 1))


@dataclass
class _Row:
    """Stand-in for one ``auth_throttle_buckets`` row: just the counters."""

    failures: int = 0
    blocked_until: datetime | None = None
    last_charged_at: datetime | None = None
    drains_at: datetime | None = None
    expires_at: datetime = T0

    def snapshot(self) -> tuple[object, ...]:
        return dataclasses.astuple(self)


def _orm(row: _Row) -> AuthThrottleBucket:
    return cast("AuthThrottleBucket", row)


def _is_open(row: _Row, kind: BucketKind, now: datetime) -> bool:
    return module._is_open(_orm(row), POLICIES[kind], now)  # noqa: SLF001


def _charge(row: _Row, target: Bucket, now: datetime) -> module._Charge:
    return module._charge(_orm(row), target, now)  # noqa: SLF001


def _refund(row: _Row, charge: module._Charge, now: datetime) -> None:
    module._refund(_orm(row), charge, now)  # noqa: SLF001


def _clear(row: _Row, now: datetime) -> None:
    module._clear(_orm(row), now)  # noqa: SLF001


def _backoff(kind: BucketKind) -> Backoff:
    policy = POLICIES[kind]
    assert isinstance(policy, Backoff)
    return policy


def _budget(kind: BucketKind) -> Budget:
    policy = POLICIES[kind]
    assert isinstance(policy, Budget)
    return policy


def _fail_until_blocked(row: _Row, target: Bucket, now: datetime) -> datetime:
    """Charge wrong attempts, each the instant it is allowed, until one starts a wait."""
    while True:
        assert _is_open(row, target.kind, now)
        _charge(row, target, now)
        if row.blocked_until is not None:
            return now


def _evaluations_available(row: _Row, target: Bucket, now: datetime) -> int:
    """How many attempts a budget bucket would admit at one instant (on a copy)."""
    copy = dataclasses.replace(row)
    admitted = 0
    while _is_open(copy, target.kind, now):
        _charge(copy, target, now)
        admitted += 1
        assert admitted < 10_000
    return admitted


# ── The policy itself ────────────────────────────────────────────────────────


class TestPolicyTable:
    """Attack: weaken the throttle by editing a number, or by configuration."""

    #: The exact policy. A change that loosens any of these must be made here
    #: too, on purpose, where a reviewer sees it.
    EXPECTED: dict[BucketKind, Backoff | Budget] = {  # noqa: RUF012
        BucketKind.PW_SOURCE: Budget(burst=300, refill=timedelta(seconds=15)),
        BucketKind.PW_PAIR: Backoff(free=5, base=MINUTE, cap=30 * MINUTE, quiet=2 * HOUR),
        BucketKind.PW_ACCOUNT: Budget(burst=20, refill=15 * MINUTE),
        BucketKind.PW_DEVICE: Backoff(free=5, base=MINUTE, cap=30 * MINUTE, quiet=2 * HOUR),
        BucketKind.PW_DEVICE_CAP: Budget(burst=10, refill=30 * MINUTE),
        BucketKind.MFA_ORIGIN: Backoff(free=5, base=MINUTE, cap=HOUR, quiet=4 * HOUR),
        BucketKind.MFA_ACCOUNT: Budget(burst=10, refill=30 * MINUTE),
        BucketKind.MFA_DEVICE_CAP: Budget(burst=10, refill=30 * MINUTE),
        BucketKind.SESSION_PW: Backoff(free=5, base=MINUTE, cap=30 * MINUTE, quiet=2 * HOUR),
        BucketKind.SESSION_CODE: Backoff(free=5, base=MINUTE, cap=HOUR, quiet=4 * HOUR),
        BucketKind.INVITE_RESEND: Budget(burst=3, refill=10 * MINUTE),
        # Patient App (docs/modules/15-patient-app.md §5): used by the
        # patient services only, and all budgets — a code that was sent was
        # paid for, so nothing is ever cleared by a success.
        BucketKind.PT_OTP_SEND_SOURCE: Budget(burst=30, refill=MINUTE),
        BucketKind.PT_OTP_SEND_PAIR: Budget(burst=3, refill=10 * MINUTE),
        BucketKind.PT_OTP_SEND_PHONE: Budget(burst=6, refill=5 * MINUTE),
        BucketKind.PT_OTP_SEND_DEVICE: Budget(burst=3, refill=10 * MINUTE),
        BucketKind.PT_OTP_SEND_GLOBAL: Budget(burst=600, refill=timedelta(seconds=1)),
        BucketKind.PT_OTP_VERIFY_SOURCE: Budget(burst=60, refill=timedelta(seconds=30)),
        BucketKind.PT_LINK_ATTEMPT: Budget(burst=5, refill=12 * MINUTE),
        # Ten link attempts a day on top of five an hour: one back every
        # 144 minutes is ten in twenty-four hours.
        BucketKind.PT_LINK_DAILY: Budget(burst=10, refill=144 * MINUTE),
    }

    def test_every_number_is_exactly_the_reviewed_one(self) -> None:
        assert dict(POLICIES) == self.EXPECTED

    def test_a_verified_devices_code_budget_is_a_ceiling_no_larger_than_the_accounts(self) -> None:
        """Attack: on a stolen MFA-verified browser, guess codes without limit.

        ``mfa_device_cap`` is what an owner's own device draws on instead of
        the shared ``mfa_account``. It must be a *budget* (a success cannot
        clear it), and never more generous than the shared one it replaces:
        10 codes, then 48 a day, per device.
        """
        device = POLICIES[BucketKind.MFA_DEVICE_CAP]
        account = _budget(BucketKind.MFA_ACCOUNT)

        assert device == Budget(burst=10, refill=30 * MINUTE)
        assert isinstance(device, Budget)
        assert device.burst <= account.burst
        assert device.refill >= account.refill
        assert device.burst + timedelta(days=1) // device.refill == 10 + 48
        # Against a six-digit code, a day of guessing on one device is hopeless.
        assert (device.burst + timedelta(days=1) // device.refill) / 10**6 < 1e-4

    def test_the_kinds_are_exactly_the_reviewed_ones_in_lock_order(self) -> None:
        """Declaration order is lock order: inserting a kind anywhere else reorders every lock."""
        assert [kind.value for kind in BucketKind] == [
            "pw_source",
            "pw_pair",
            "pw_device",
            "pw_device_cap",
            "pw_account",
            "mfa_origin",
            "mfa_device_cap",
            "mfa_account",
            "session_pw",
            "session_code",
            "invite_resend",
            # Appended: the staff kinds above keep their lock order.
            "pt_otp_send_source",
            "pt_otp_send_pair",
            "pt_otp_send_phone",
            "pt_otp_send_device",
            "pt_otp_send_global",
            "pt_otp_verify_source",
            "pt_link_attempt",
            "pt_link_daily",
        ]
        assert [kind for kind, _ in sorted(module._LOCK_ORDER.items(), key=lambda i: i[1])] == list(  # noqa: SLF001
            BucketKind
        )

    def test_every_bucket_kind_has_a_policy(self) -> None:
        """A kind with no policy would be a counter nothing limits."""
        assert set(POLICIES) == set(BucketKind)

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    def test_a_backoff_cannot_forget_itself_between_attempts_at_the_cap(
        self, kind: BucketKind
    ) -> None:
        """Attack: wait out the cap, guess, repeat — and have each guess count as the first.

        If ``quiet`` were not longer than ``cap``, a bucket sitting at its
        longest wait would have reset by the time the wait ended.
        """
        policy = _backoff(kind)
        assert policy.quiet > policy.cap
        assert policy.cap >= policy.base > timedelta(0)
        assert policy.free >= 1

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    def test_the_doubling_limit_is_far_past_every_cap(self, kind: BucketKind) -> None:
        policy = _backoff(kind)
        assert policy.base * (2**module._MAX_DOUBLINGS) > policy.cap  # noqa: SLF001

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_a_budget_is_finite_and_refills(self, kind: BucketKind) -> None:
        policy = _budget(kind)
        assert policy.burst >= 1
        assert policy.refill > timedelta(0)

    def test_the_account_wide_budgets_bound_a_day_of_guessing(self) -> None:
        """However many addresses an attacker has: 20 + 96 passwords, 10 + 48 codes a day."""
        day = timedelta(days=1)
        password = _budget(BucketKind.PW_ACCOUNT)
        code = _budget(BucketKind.MFA_ACCOUNT)

        assert password.burst + day // password.refill == 20 + 96
        assert code.burst + day // code.refill == 10 + 48

    def test_a_policy_cannot_be_changed_at_runtime(self) -> None:
        for policy in POLICIES.values():
            field = dataclasses.fields(policy)[0].name
            with pytest.raises(dataclasses.FrozenInstanceError):
                setattr(policy, field, 10**6)

    def test_the_throttle_reads_no_setting(self) -> None:
        """Attack: switch the throttle off, or loosen it, from the environment.

        The module must not so much as import the settings object: every
        number is a constant in code.
        """
        source = inspect.getsource(module)

        assert "app.core.config" not in source
        assert "settings" not in vars(module)
        assert "getenv" not in source
        assert "environ" not in source

    def test_no_setting_names_a_lockout_or_a_throttle_number(self) -> None:
        """The old lockout knobs are gone, and nothing has replaced them."""
        for removed in ("MAX_FAILED_LOGIN_ATTEMPTS", "ACCOUNT_LOCKOUT_MINUTES"):
            assert removed not in Settings.model_fields
            assert not hasattr(settings, removed)

        suspicious = [
            name
            for name in Settings.model_fields
            if any(word in name for word in ("THROTTLE", "LOCKOUT", "BACKOFF", "FAILED_LOGIN"))
        ]
        assert suspicious == []

    def test_changing_every_numeric_setting_changes_no_policy(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        before = dict(POLICIES)
        for name in Settings.model_fields:
            value = getattr(settings, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                continue
            monkeypatch.setattr(settings, name, type(value)(10**6))

        target = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        row = _Row()
        _fail_until_blocked(row, target, T0)

        assert dict(POLICIES) == before
        assert row.failures == 5
        assert row.blocked_until == T0 + MINUTE


# ── Backoff ──────────────────────────────────────────────────────────────────


class TestBackoffSchedule:
    """Attack: guess a password, or a code, as fast as the server will answer."""

    @pytest.mark.parametrize(
        ("kind", "expected_waits"),
        [
            (BucketKind.PW_PAIR, [0, 0, 0, 0, 60, 120, 240, 480, 960, 1800, 1800, 1800]),
            (BucketKind.PW_DEVICE, [0, 0, 0, 0, 60, 120, 240, 480, 960, 1800, 1800, 1800]),
            (BucketKind.SESSION_PW, [0, 0, 0, 0, 60, 120, 240, 480, 960, 1800, 1800, 1800]),
            (BucketKind.MFA_ORIGIN, [0, 0, 0, 0, 60, 120, 240, 480, 960, 1920, 3600, 3600]),
            (BucketKind.SESSION_CODE, [0, 0, 0, 0, 60, 120, 240, 480, 960, 1920, 3600, 3600]),
        ],
    )
    def test_five_free_then_a_minute_doubling_to_the_cap(
        self, kind: BucketKind, expected_waits: list[int]
    ) -> None:
        """Each wrong attempt is made the instant the previous wait ends."""
        target = bucket(kind, "x")
        row = _Row()
        now = T0
        waits: list[int] = []

        for attempt in range(1, len(expected_waits) + 1):
            assert _is_open(row, kind, now), f"attempt {attempt} was refused"
            charge = _charge(row, target, now)
            waits.append(charge.wait_started)
            assert row.failures == attempt
            if charge.wait_started:
                assert row.blocked_until == now + timedelta(seconds=charge.wait_started)
                now = row.blocked_until
            else:
                assert row.blocked_until is None

        assert waits == expected_waits

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    def test_exactly_the_free_attempts_are_evaluated_at_once(self, kind: BucketKind) -> None:
        """A burst at one instant: the free attempts and not one more."""
        target = bucket(kind, "x")
        row = _Row()
        evaluated = 0
        for _ in range(50):
            if _is_open(row, kind, T0):
                _charge(row, target, T0)
                evaluated += 1

        assert evaluated == _backoff(kind).free

    def test_a_wait_ends_exactly_when_it_says_and_not_a_moment_before(self) -> None:
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        _fail_until_blocked(row, target, T0)
        assert row.blocked_until == T0 + MINUTE

        assert not _is_open(row, target.kind, T0)
        assert not _is_open(row, target.kind, T0 + MINUTE - timedelta(microseconds=1))
        assert _is_open(row, target.kind, T0 + MINUTE)

    def test_nothing_is_ever_locked_the_next_attempt_is_judged_once_the_wait_is_over(
        self,
    ) -> None:
        """The old lockout's flaw: an attacker keeps the owner out indefinitely.

        Here even a bucket at its longest wait opens again, on its own, with
        no administrator and no reset.
        """
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        now = T0
        for _ in range(40):
            _charge(row, target, now)
            assert row.blocked_until is None or row.blocked_until - now <= 30 * MINUTE
            if row.blocked_until is not None:
                now = row.blocked_until

        assert _is_open(row, target.kind, now)

    def test_the_wait_never_exceeds_the_cap_however_many_failures(self) -> None:
        """No overflow, and no wait an attacker can grow without bound."""
        target = bucket(BucketKind.MFA_ORIGIN, "x")
        row = _Row(failures=10**9, last_charged_at=T0)

        charge = _charge(row, target, T0 + SECOND)

        assert charge.wait_started == 3600
        assert row.blocked_until == T0 + SECOND + HOUR


class TestBackoffQuietReset:
    def test_a_bucket_left_alone_for_the_quiet_period_starts_over(self) -> None:
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        last = _fail_until_blocked(row, target, T0)

        charge = _charge(row, target, last + 2 * HOUR)

        assert row.failures == 1
        assert row.blocked_until is None
        assert charge.wait_started == 0

    def test_one_microsecond_short_of_quiet_is_not_quiet(self) -> None:
        """Attack: pace guesses just inside the quiet period and never escalate."""
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        last = _fail_until_blocked(row, target, T0)
        almost = last + 2 * HOUR - timedelta(microseconds=1)

        charge = _charge(row, target, almost)

        assert row.failures == 6
        assert charge.wait_started == 120

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    def test_guessing_the_instant_each_wait_ends_never_resets_the_count(
        self, kind: BucketKind
    ) -> None:
        """The patient attacker: at the cap, every further guess still costs the cap."""
        target = bucket(kind, "x")
        policy = _backoff(kind)
        row = _Row()
        now = T0
        for _ in range(30):
            _charge(row, target, now)
            if row.blocked_until is not None:
                now = row.blocked_until

        assert row.failures == 30
        assert row.blocked_until == row.last_charged_at + policy.cap  # type: ignore[operator]

    def test_a_charged_bucket_is_kept_for_the_whole_quiet_period(self) -> None:
        """The purge must not delete a bucket that still remembers failures."""
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()

        _charge(row, target, T0)

        assert row.expires_at == T0 + 2 * HOUR
        assert row.expires_at > T0 + _backoff(target.kind).cap


# ── Budget ───────────────────────────────────────────────────────────────────


class TestBudget:
    """Attack: guess one account's password from as many addresses as it takes."""

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_exactly_the_burst_is_admitted_at_one_instant(self, kind: BucketKind) -> None:
        assert _evaluations_available(_Row(), bucket(kind, "x"), T0) == _budget(kind).burst

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_an_exhausted_budget_gives_one_attempt_per_refill_and_no_more(
        self, kind: BucketKind
    ) -> None:
        target = bucket(kind, "x")
        policy = _budget(kind)
        row = _Row()
        for _ in range(policy.burst):
            _charge(row, target, T0)

        assert not _is_open(row, kind, T0)
        assert not _is_open(row, kind, T0 + policy.refill - timedelta(microseconds=1))
        assert _evaluations_available(row, target, T0 + policy.refill) == 1
        assert _evaluations_available(row, target, T0 + 3 * policy.refill) == 3

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_a_budget_never_holds_more_than_its_burst(self, kind: BucketKind) -> None:
        """Attack: stay away for a year and come back to a year's worth of guesses."""
        target = bucket(kind, "x")
        row = _Row()
        _charge(row, target, T0)

        year_later = T0 + timedelta(days=365)

        assert _evaluations_available(row, target, year_later) == _budget(kind).burst

    def test_a_day_of_guessing_one_account_from_everywhere_is_bounded(self) -> None:
        """Every address on the internet together: 20, then one per 15 minutes."""
        target = bucket(BucketKind.PW_ACCOUNT, "victim@hospital.example")
        row = _Row()
        admitted = 0
        now = T0
        while now < T0 + timedelta(days=1):
            if _is_open(row, target.kind, now):
                _charge(row, target, now)
                admitted += 1
            now += timedelta(seconds=30)

        assert admitted == 20 + 95  # the 96th refill lands exactly at the day's end

    def test_a_budget_charge_reports_no_wait_and_no_failures(self) -> None:
        charge = _charge(_Row(), bucket(BucketKind.PW_ACCOUNT, "x"), T0)

        assert charge.wait_started == 0
        assert charge.failures_after == 0
        assert charge.exhausted is False

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_exactly_the_charge_that_takes_the_last_unit_reports_the_budget_exhausted(
        self, kind: BucketKind
    ) -> None:
        """The audit trail's one trace that an allowance ran out.

        After this charge further attempts are refused unevaluated and write
        nothing, so the last evaluated attempt must say so — and only that one.
        """
        target = bucket(kind, "x")
        row = _Row()

        reports = [_charge(row, target, T0).exhausted for _ in range(_budget(kind).burst)]

        assert reports == [False] * (_budget(kind).burst - 1) + [True]
        assert not _is_open(row, kind, T0)

    async def test_an_admission_names_the_budgets_it_exhausted_and_only_those(self) -> None:
        throttle, _, _ = _throttle()
        account = bucket(BucketKind.MFA_ACCOUNT, "user")
        origin = bucket(BucketKind.MFA_ORIGIN, "user", "source", "203.0.113.7")
        other_origins = [bucket(BucketKind.MFA_ORIGIN, "user", "source", str(i)) for i in range(9)]

        earlier = [await throttle.admit([account, other]) for other in other_origins]
        last = await throttle.admit([account, origin])
        refused = await throttle.admit([account, origin])

        assert all(admission.budgets_exhausted == () for admission in earlier)
        assert last.admitted is True
        assert last.budgets_exhausted == (BucketKind.MFA_ACCOUNT,)
        assert refused.admitted is False
        assert refused.budgets_exhausted == ()

    def test_a_charged_budget_is_kept_until_it_is_full_again(self) -> None:
        target = bucket(BucketKind.PW_ACCOUNT, "x")
        row = _Row()
        for _ in range(3):
            _charge(row, target, T0)

        assert row.drains_at == T0 + 45 * MINUTE
        assert row.expires_at == row.drains_at


# ── Refund ───────────────────────────────────────────────────────────────────


class TestRefund:
    """Attack: turn a correct credential into more than it was charged."""

    @pytest.mark.parametrize("prior_failures", [0, 1, 3, 4, 5, 9])
    def test_a_backoff_refund_puts_the_bucket_back_exactly(self, prior_failures: int) -> None:
        """Charge then refund, with nobody in between: as if it never happened."""
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        now = T0
        for _ in range(prior_failures):
            _charge(row, target, now)
            now = row.blocked_until or now + SECOND
        before = (row.failures, row.last_charged_at)
        was_open_at = now

        charge = _charge(row, target, now)
        _refund(row, charge, now + SECOND)

        assert (row.failures, row.last_charged_at) == (before if prior_failures else (0, None))
        # The wait the refunded attempt started is gone; no earlier one is revived.
        assert row.blocked_until is None
        assert _is_open(row, target.kind, was_open_at)

    def test_the_owner_typing_the_right_password_does_not_escalate_the_wait(self) -> None:
        """Four typos, then the right password, then a typo: still no wait.

        Without the refund the correct attempt would be the fifth "failure"
        and a later typo would start at two minutes.
        """
        target = bucket(BucketKind.PW_DEVICE, "x")
        row = _Row()
        for _ in range(4):
            _charge(row, target, T0)

        correct = _charge(row, target, T0)
        assert correct.wait_started == 60
        _refund(row, correct, T0)

        assert row.failures == 4
        assert row.blocked_until is None
        typo = _charge(row, target, T0 + SECOND)
        assert typo.wait_started == 60  # the fifth, not the sixth

    def test_a_refund_never_takes_the_count_below_zero(self) -> None:
        """Attack: replay a settlement to bank negative failures as extra free attempts."""
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        charge = _charge(row, target, T0)

        for _ in range(5):
            _refund(row, charge, T0)

        assert row.failures == 0
        assert row.blocked_until is None
        burst = 0
        while _is_open(row, target.kind, T0):
            _charge(row, target, T0)
            burst += 1
        assert burst == 5

    def test_a_refund_after_someone_else_was_charged_only_takes_one_off(self) -> None:
        """Attack: guess wrong, in parallel with a request that has the right password.

        The correct attempt was charged first (failure 4); the attacker's
        wrong one was charged after it (failure 5) and started a wait. The
        refund takes exactly the owner's one unit off and leaves the bucket
        precisely as if the owner had never been there: four wrong attempts
        counted, the attacker's last free one spent, and the very next wrong
        attempt starting the first wait. The attacker gains no evaluation.
        """
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        for _ in range(3):
            _charge(row, target, T0)
        owner = _charge(row, target, T0)
        attacker = _charge(row, target, T0)
        assert attacker.wait_started == 60

        _refund(row, owner, T0 + SECOND)

        without_the_owner = _Row()
        for _ in range(4):
            _charge(without_the_owner, target, T0)
        assert (row.failures, row.blocked_until, row.last_charged_at) == (
            without_the_owner.failures,
            without_the_owner.blocked_until,
            without_the_owner.last_charged_at,
        )
        assert (row.failures, row.blocked_until, row.last_charged_at) == (4, None, T0)
        # One more evaluation, and it starts the wait: five wrong in all, as the policy says.
        assert _charge(row, target, T0 + SECOND).wait_started == 60
        assert not _is_open(row, target.kind, T0 + SECOND)

    def test_a_stale_refund_leaves_exactly_the_failures_that_are_still_counted(self) -> None:
        """A correct attempt settled late, after four wrong ones were charged behind it.

        Five were counted and the fifth started a wait; one of the five was
        not a failure at all. With it given back there are four — fewer than
        the free allowance — so there is nothing to wait for, and the bucket
        is exactly where four wrong attempts alone would have left it.
        """
        target = bucket(BucketKind.MFA_ORIGIN, "x")
        row = _Row()
        stale = _charge(row, target, T0)
        for _ in range(4):
            _charge(row, target, T0)
        blocked_before = row.snapshot()[1]
        assert blocked_before == T0 + MINUTE

        _refund(row, stale, T0 + SECOND)

        assert row.snapshot()[:3] == (4, None, T0)
        # The count is not forgotten: the next wrong code is the fifth.
        assert _charge(row, target, T0 + SECOND).wait_started == 60

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    @pytest.mark.parametrize("first_to_settle", ["earlier", "later"])
    @pytest.mark.parametrize("prior_failures", [0, 1, 2, 3])
    def test_two_simultaneous_correct_attempts_leave_no_wait_behind(
        self, kind: BucketKind, first_to_settle: str, prior_failures: int
    ) -> None:
        """The owner double-clicks "Sign in" after a few typos: both requests are right.

        Both are charged before either is judged; with three typos already
        counted, the second charge is the fifth and starts a wait. Both are
        refunded — in either order — and the bucket must be exactly as it was
        before them: no wait that nobody's failure earned.
        """
        target = bucket(kind, "x")
        row = _Row()
        for _ in range(prior_failures):
            _charge(row, target, T0)
        before = (row.failures, row.blocked_until, row.last_charged_at)

        earlier = _charge(row, target, T0 + SECOND)
        later = _charge(row, target, T0 + SECOND)
        assert bool(later.wait_started) == (prior_failures == 3)
        order = (earlier, later) if first_to_settle == "earlier" else (later, earlier)
        for charge in order:
            _refund(row, charge, T0 + 2 * SECOND)

        assert row.failures == prior_failures
        assert row.blocked_until is None
        assert _is_open(row, kind, T0 + 2 * SECOND)
        if first_to_settle == "later":
            # Unwound in reverse, nothing at all is left of the two attempts.
            assert (row.failures, row.blocked_until, row.last_charged_at) == before
        # The free allowance is what it was: no more, no fewer.
        free_left = 0
        while _is_open(row, kind, T0 + 2 * SECOND):
            _charge(row, target, T0 + 2 * SECOND)
            free_left += 1
        assert free_left == _backoff(kind).free - prior_failures

    @pytest.mark.parametrize("kind", BACKOFF_KINDS)
    @pytest.mark.parametrize("wrong_after", [1, 2, 3, 6])
    def test_a_refund_never_removes_a_wait_that_counted_failures_still_earn(
        self, kind: BucketKind, wrong_after: int
    ) -> None:
        """Attack: guess in parallel with a correct attempt and let its refund lift your wait.

        Four wrong attempts, then the owner's correct one (charged as the
        fifth, starting a wait), then — each the instant the wait before it
        ends — more wrong ones. The owner's refund arrives late. Five or more
        failures are still counted without it, so the wait stands, to the
        instant, and nothing is evaluated before it ends.
        """
        target = bucket(kind, "x")
        policy = _backoff(kind)
        row = _Row()
        for _ in range(policy.free - 1):
            _charge(row, target, T0)
        owner = _charge(row, target, T0)
        now = T0
        for _ in range(wrong_after):
            assert row.blocked_until is not None
            now = row.blocked_until
            _charge(row, target, now)
        blocked_until = row.blocked_until
        assert blocked_until is not None and blocked_until > now

        _refund(row, owner, now + SECOND)

        assert row.failures == policy.free - 1 + wrong_after >= policy.free
        assert row.blocked_until == blocked_until
        assert row.last_charged_at == now
        assert not _is_open(row, kind, now + SECOND)
        assert not _is_open(row, kind, blocked_until - timedelta(microseconds=1))

    def test_a_refund_at_exactly_the_free_allowance_keeps_the_wait(self) -> None:
        """The boundary: ``failures == free`` after the refund is still a wait earned."""
        target = bucket(BucketKind.PW_PAIR, "x")
        row = _Row()
        for _ in range(3):
            _charge(row, target, T0)
        owner = _charge(row, target, T0)  # 4
        _charge(row, target, T0)  # 5: wait until T0 + 1 min
        _charge(row, target, T0 + MINUTE)  # 6: wait until T0 + 3 min

        _refund(row, owner, T0 + MINUTE + SECOND)

        assert row.failures == 5
        assert row.blocked_until == T0 + 3 * MINUTE

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_a_budget_refund_gives_back_exactly_one_unit(self, kind: BucketKind) -> None:
        target = bucket(kind, "x")
        row = _Row()
        for _ in range(_budget(kind).burst - 1):
            _charge(row, target, T0)
        available = _evaluations_available(row, target, T0)
        assert available == 1

        charge = _charge(row, target, T0)
        assert _evaluations_available(row, target, T0) == 0
        _refund(row, charge, T0)

        assert _evaluations_available(row, target, T0) == 1

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    def test_a_budget_refund_never_creates_more_than_the_burst(self, kind: BucketKind) -> None:
        """Attack: a valid login, settled over and over, to pump up a shared budget."""
        target = bucket(kind, "x")
        row = _Row()
        charge = _charge(row, target, T0)

        for _ in range(1000):
            _refund(row, charge, T0)

        assert _evaluations_available(row, target, T0) == _budget(kind).burst
        assert _evaluations_available(row, target, T0 + HOUR) == _budget(kind).burst

    def test_a_budget_refund_leaves_other_peoples_charges_in_place(self) -> None:
        """Somebody else signing in never hands the attacker a fresh allowance."""
        target = bucket(BucketKind.PW_ACCOUNT, "victim@hospital.example")
        row = _Row()
        for _ in range(19):  # the attacker
            _charge(row, target, T0)
        owner = _charge(row, target, T0)  # the 20th and last unit
        assert not _is_open(row, target.kind, T0)

        _refund(row, owner, T0)

        assert _evaluations_available(row, target, T0) == 1

    def test_a_refund_on_a_budget_row_that_was_never_charged_is_harmless(self) -> None:
        target = bucket(BucketKind.PW_ACCOUNT, "x")
        charge = _charge(_Row(), target, T0)
        fresh = _Row()

        _refund(fresh, charge, T0)

        assert fresh.drains_at is None
        assert _evaluations_available(fresh, target, T0) == 20


class TestClear:
    def test_clear_returns_a_backoff_to_its_starting_state(self) -> None:
        target = bucket(BucketKind.PW_DEVICE, "x")
        row = _Row()
        _fail_until_blocked(row, target, T0)

        _clear(row, T0 + SECOND)

        assert (row.failures, row.blocked_until, row.last_charged_at) == (0, None, None)
        assert row.expires_at == T0 + SECOND
        assert _is_open(row, target.kind, T0 + SECOND)

    def test_after_a_clear_the_schedule_starts_from_the_beginning(self) -> None:
        target = bucket(BucketKind.PW_DEVICE, "x")
        row = _Row()
        now = T0
        for _ in range(9):
            _charge(row, target, now)
            now = row.blocked_until or now
        _clear(row, now)

        waits = [_charge(row, target, now).wait_started for _ in range(5)]

        assert waits == [0, 0, 0, 0, 60]


# ── Bucket names ─────────────────────────────────────────────────────────────


class TestBucketKeys:
    """Attack: make two different things share a counter, or one thing use two."""

    def test_the_key_is_a_sha256_and_names_nothing(self) -> None:
        email = "asha.rao@hospital.example"
        named = bucket(BucketKind.PW_ACCOUNT, email)

        assert len(named.key_hash) == 64
        assert set(named.key_hash) <= set("0123456789abcdef")
        assert "asha" not in named.key_hash
        assert named.kind is BucketKind.PW_ACCOUNT
        assert named.policy is POLICIES[BucketKind.PW_ACCOUNT]

    def test_the_same_parts_always_name_the_same_bucket(self) -> None:
        """Every worker, and every restart, must count in the same row."""
        first = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        second = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")

        assert first == second
        assert hash(first) == hash(second)

    def test_the_key_is_pinned_so_a_deploy_cannot_silently_reset_every_counter(self) -> None:
        import hashlib

        digest = hashlib.sha256(b"aetheris:auth-throttle:v1")
        for part in (b"pw_account", b"a@hospital.example"):
            digest.update(len(part).to_bytes(4, "big"))
            digest.update(part)

        assert bucket(BucketKind.PW_ACCOUNT, "a@hospital.example").key_hash == digest.hexdigest()

    def test_moving_a_character_between_parts_changes_the_bucket(self) -> None:
        """Attack: pick an email and a source whose concatenation matches someone else's."""
        assert bucket(BucketKind.PW_PAIR, "ab", "c") != bucket(BucketKind.PW_PAIR, "a", "bc")
        assert bucket(BucketKind.PW_PAIR, "abc") != bucket(BucketKind.PW_PAIR, "ab", "c")
        assert bucket(BucketKind.PW_PAIR, "a", "") != bucket(BucketKind.PW_PAIR, "a")
        assert bucket(BucketKind.PW_PAIR, "", "a") != bucket(BucketKind.PW_PAIR, "a", "")

    def test_a_part_cannot_imitate_the_length_prefix_of_another(self) -> None:
        crafted = "a" + "\x00\x00\x00\x01" + "b"
        assert bucket(BucketKind.PW_PAIR, crafted) != bucket(BucketKind.PW_PAIR, "a", "b")

    def test_distinct_parts_give_distinct_keys(self) -> None:
        emails = [f"user{i}@hospital.example" for i in range(50)]
        sources = [f"203.0.113.{i}" for i in range(50)]

        keys = {bucket(BucketKind.PW_PAIR, e, s).key_hash for e in emails for s in sources}

        assert len(keys) == 2500

    def test_the_kind_is_part_of_the_key(self) -> None:
        """One user id must not be one counter for every purpose."""
        user_id = uuid.uuid4()

        keys = {bucket(kind, user_id).key_hash for kind in BucketKind}

        assert len(keys) == len(BucketKind)

    def test_a_uuid_and_its_text_name_the_same_bucket(self) -> None:
        user_id = uuid.uuid4()
        assert bucket(BucketKind.MFA_ACCOUNT, user_id) == bucket(
            BucketKind.MFA_ACCOUNT, str(user_id)
        )

    def test_non_ascii_parts_are_keyed_by_their_bytes(self) -> None:
        assert bucket(BucketKind.PW_ACCOUNT, "é") != bucket(BucketKind.PW_ACCOUNT, "e")
        assert bucket(BucketKind.PW_ACCOUNT, "é") != bucket(BucketKind.PW_ACCOUNT, "e", "́")


# ── Lock order ───────────────────────────────────────────────────────────────


class _MemoryBuckets:
    """In-memory stand-in for ``AuthThrottleRepository``: rows in a dict, a settable clock."""

    def __init__(self) -> None:
        self.rows: dict[str, _Row] = {}
        self.clock = T0
        self.locked: list[tuple[str, str]] = []
        self.flushes = 0
        self.purges: list[tuple[datetime, int]] = []

    async def now(self) -> datetime:
        return self.clock

    async def lock(self, kind: str, key_hash: str, *, now: datetime) -> _Row:
        self.locked.append((kind, key_hash))
        return self.rows.setdefault(key_hash, _Row(expires_at=now))

    async def flush(self) -> None:
        self.flushes += 1

    async def purge_expired(self, *, now: datetime, limit: int) -> int:
        self.purges.append((now, limit))
        return 0

    def state(self) -> dict[str, tuple[object, ...]]:
        return {key: row.snapshot() for key, row in self.rows.items()}


def _throttle() -> tuple[AuthThrottle, _MemoryBuckets, AsyncMock]:
    buckets, uow = _MemoryBuckets(), AsyncMock()
    return AuthThrottle(buckets, uow), buckets, uow  # type: ignore[arg-type]


def _lock_key(target: Bucket) -> tuple[int, str]:
    return (module._LOCK_ORDER[target.kind], target.key_hash)  # noqa: SLF001


class TestLockOrder:
    """Attack: two parallel attempts that lock the same buckets in opposite orders."""

    def test_the_order_is_total_over_every_kind(self) -> None:
        order = module._LOCK_ORDER  # noqa: SLF001

        assert set(order) == set(BucketKind)
        assert sorted(order.values()) == list(range(len(BucketKind)))

    def test_any_two_distinct_buckets_are_strictly_ordered(self) -> None:
        targets = [bucket(kind, part) for kind in BucketKind for part in ("a", "b", "c")]

        for first, second in itertools.combinations(targets, 2):
            assert _lock_key(first) != _lock_key(second)
            assert (_lock_key(first) < _lock_key(second)) != (_lock_key(second) < _lock_key(first))

    async def test_admission_locks_in_the_same_order_whatever_order_it_is_given(self) -> None:
        targets = [
            bucket(BucketKind.PW_ACCOUNT, "a@hospital.example"),
            bucket(BucketKind.PW_SOURCE, "203.0.113.7"),
            bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7"),
            bucket(BucketKind.PW_PAIR, "b@hospital.example", "203.0.113.7"),
        ]
        expected = [(t.kind.value, t.key_hash) for t in sorted(targets, key=_lock_key)]

        for permutation in itertools.permutations(targets):
            throttle, buckets, _ = _throttle()
            await throttle.admit(list(permutation))
            assert buckets.locked == expected

    async def test_settlement_locks_in_that_same_order(self) -> None:
        targets = [
            bucket(BucketKind.PW_DEVICE_CAP, "device"),
            bucket(BucketKind.PW_DEVICE, "device"),
        ]
        throttle, buckets, _ = _throttle()
        admission = await throttle.admit(targets)
        buckets.locked.clear()

        await throttle.settle(admission, clear=[targets[1]])

        assert buckets.locked == [
            (t.kind.value, t.key_hash) for t in sorted(targets, key=_lock_key)
        ]


# ── Admission and settlement over the stand-in repository ────────────────────


class TestAdmission:
    async def test_an_admitted_attempt_is_charged_to_every_bucket_before_it_is_evaluated(
        self,
    ) -> None:
        """Charge first: an attempt that crashes half-way stays counted."""
        throttle, buckets, uow = _throttle()
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")

        admission = await throttle.admit([account, pair])

        assert admission.admitted is True
        assert admission.refused_by is None
        assert {charge.bucket for charge in admission.charges} == {account, pair}
        assert buckets.rows[account.key_hash].drains_at == T0 + 15 * MINUTE
        assert buckets.rows[pair.key_hash].failures == 1
        assert uow.commit.await_count >= 1

    async def test_a_refusal_changes_nothing_in_any_bucket(self) -> None:
        """Attack: hammer a closed bucket to drain the account's shared budget.

        The pair backoff is closed. Thousands of refused attempts must not
        take a single unit from the account budget (locked after it or before
        it) or lengthen the wait — or the attacker could shut the owner out
        from anywhere, which is the lockout this design replaced.
        """
        throttle, buckets, uow = _throttle()
        source = bucket(BucketKind.PW_SOURCE, "203.0.113.7")
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")
        for _ in range(5):
            assert (await throttle.admit([source, pair, account])).admitted
        before = buckets.state()
        commits_before = uow.commit.await_count

        for _ in range(2000):
            refusal = await throttle.admit([source, pair, account])
            assert refusal == Admission(admitted=False, refused_by=BucketKind.PW_PAIR)

        assert buckets.state() == before
        assert uow.commit.await_count == commits_before + 2000  # every lock was released

    @pytest.mark.parametrize("refused", [False, True])
    async def test_refusals_sweep_expired_rows_as_admissions_do(
        self, monkeypatch: pytest.MonkeyPatch, refused: bool
    ) -> None:
        """Attack: flood refused attempts under ever-new keys to fill the bucket table.

        A row created for a refused attempt holds nothing and has already
        expired. If only admitted attempts ever swept, a stream of refusals
        would add rows faster than anything removed them. The sweep changes
        no live bucket and its lock is released by a commit of its own.
        """
        monkeypatch.setattr(module, "secrets", SimpleNamespace(randbelow=lambda _n: 0))
        throttle, buckets, uow = _throttle()
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        for _ in range(5 if refused else 4):
            await throttle.admit([pair])
        buckets.purges.clear()
        before = buckets.state()
        commits = uow.commit.await_count

        admission = await throttle.admit([pair])

        assert admission.admitted is not refused
        assert buckets.purges == [(T0, module._PURGE_BATCH)]  # noqa: SLF001
        assert uow.commit.await_count == commits + 2
        if refused:
            assert buckets.state() == before

    def test_the_sweep_is_bounded_and_occasional(self) -> None:
        """One request must never be made to delete an unbounded number of rows."""
        assert 0 < module._PURGE_BATCH <= 1000  # noqa: SLF001
        assert module._PURGE_ONE_IN >= 8  # noqa: SLF001

    async def test_a_refusal_by_a_later_bucket_does_not_charge_the_earlier_ones(self) -> None:
        """All or nothing: the source budget is locked first and must stay untouched."""
        throttle, buckets, _ = _throttle()
        source = bucket(BucketKind.PW_SOURCE, "203.0.113.7")
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")
        for _ in range(20):
            await throttle.admit([account])
        source_before = buckets.rows.get(source.key_hash)
        assert source_before is None

        refusal = await throttle.admit([source, account])

        assert refusal.admitted is False
        assert refusal.refused_by is BucketKind.PW_ACCOUNT
        assert refusal.charges == ()
        assert buckets.rows[source.key_hash].drains_at is None

    async def test_a_refused_attempt_can_be_settled_without_giving_anything_back(self) -> None:
        """Attack surface: a refusal carries no charges, so settling it is a no-op."""
        throttle, buckets, uow = _throttle()
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        for _ in range(5):
            await throttle.admit([pair])
        refusal = await throttle.admit([pair])
        before = buckets.state()
        commits = uow.commit.await_count

        await throttle.settle(refusal)

        assert buckets.state() == before
        assert uow.commit.await_count == commits

    async def test_the_owners_device_is_untouched_by_an_exhausted_account_budget(self) -> None:
        """The point of the design: outsiders cannot use up a trusted device's allowance."""
        throttle, _, _ = _throttle()
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")
        for _ in range(20):
            await throttle.admit([account])
        assert (await throttle.admit([account])).admitted is False

        device = [
            bucket(BucketKind.PW_DEVICE, "device-1"),
            bucket(BucketKind.PW_DEVICE_CAP, "device-1"),
        ]
        assert (await throttle.admit(device)).admitted is True

    async def test_the_wait_an_attempt_started_is_reported_for_the_audit_trail(self) -> None:
        throttle, _, _ = _throttle()
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")

        waits = [(await throttle.admit([pair, account])).wait_started for _ in range(5)]

        assert waits == [None, None, None, None, (BucketKind.PW_PAIR, 60)]

    async def test_time_is_the_repositorys_clock_not_the_hosts(self) -> None:
        """Two hosts with different clocks must agree on when a wait ends."""
        throttle, buckets, _ = _throttle()
        buckets.clock = datetime(2001, 1, 1, tzinfo=UTC)
        pair = bucket(BucketKind.PW_PAIR, "x")

        for _ in range(5):
            await throttle.admit([pair])

        assert await throttle.now() == buckets.clock
        assert buckets.rows[pair.key_hash].blocked_until == buckets.clock + MINUTE
        buckets.clock += MINUTE
        assert (await throttle.admit([pair])).admitted is True


class TestSettlement:
    async def test_settling_gives_every_charge_back(self) -> None:
        throttle, buckets, _ = _throttle()
        targets = [
            bucket(BucketKind.PW_SOURCE, "203.0.113.7"),
            bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7"),
            bucket(BucketKind.PW_ACCOUNT, "a@hospital.example"),
        ]
        for _ in range(4):
            await throttle.admit(targets)
        before = {
            t.key_hash: _evaluations_available(buckets.rows[t.key_hash], t, T0)
            for t in targets[::2]
        }
        failures_before = buckets.rows[targets[1].key_hash].failures

        admission = await throttle.admit(targets)
        await throttle.settle(admission)

        after = {
            t.key_hash: _evaluations_available(buckets.rows[t.key_hash], t, T0)
            for t in targets[::2]
        }
        assert after == before
        assert buckets.rows[targets[1].key_hash].failures == failures_before
        assert buckets.rows[targets[1].key_hash].blocked_until is None

    async def test_a_budget_cannot_be_cleared_through_settle(self) -> None:
        """Attack: one valid login that resets the account's shared budget.

        An attacker who holds *one* working credential (their own account's,
        say) must not be able to wipe a budget by naming it in ``clear``.
        Budgets named there are ignored and merely refunded their one unit.
        """
        throttle, buckets, _ = _throttle()
        account = bucket(BucketKind.PW_ACCOUNT, "victim@hospital.example")
        for _ in range(19):
            await throttle.admit([account])

        admission = await throttle.admit([account])
        await throttle.settle(admission, clear=[account])

        row = buckets.rows[account.key_hash]
        assert _evaluations_available(row, account, T0) == 1
        assert row.drains_at == T0 + 19 * 15 * MINUTE

    @pytest.mark.parametrize("kind", BUDGET_KINDS)
    async def test_no_budget_kind_can_be_cleared_even_without_a_charge(
        self, kind: BucketKind
    ) -> None:
        throttle, buckets, uow = _throttle()
        target = bucket(kind, "x")
        for _ in range(_budget(kind).burst):
            await throttle.admit([target])
        before = buckets.state()
        commits = uow.commit.await_count

        await throttle.settle(Admission(admitted=True), clear=[target])

        assert buckets.state() == before
        assert uow.commit.await_count == commits
        assert (await throttle.admit([target])).admitted is False

    async def test_clear_resets_only_the_named_backoff(self) -> None:
        """A success on one device clears that device, and nobody else's wait."""
        throttle, buckets, _ = _throttle()
        mine = bucket(BucketKind.PW_DEVICE, "my-device")
        other = bucket(BucketKind.PW_DEVICE, "another-device")
        cap = bucket(BucketKind.PW_DEVICE_CAP, "my-device")
        for _ in range(4):
            await throttle.admit([mine, cap])
        for _ in range(5):
            await throttle.admit([other])
        other_before = buckets.rows[other.key_hash].snapshot()

        admission = await throttle.admit([mine, cap])
        await throttle.settle(admission, clear=[mine])

        assert buckets.rows[mine.key_hash].failures == 0
        assert buckets.rows[mine.key_hash].blocked_until is None
        assert buckets.rows[other.key_hash].snapshot() == other_before

    async def test_a_success_cannot_reset_the_devices_ceiling(self) -> None:
        """Attack: on a stolen trusted device, alternate guesses with a known-good login.

        ``pw_device`` is cleared by a success; ``pw_device_cap`` is a budget
        precisely so that the same trick cannot buy unlimited guesses.
        """
        throttle, _, _ = _throttle()
        device = bucket(BucketKind.PW_DEVICE, "device")
        cap = bucket(BucketKind.PW_DEVICE_CAP, "device")
        wrong = 0
        for _ in range(100):
            guess = await throttle.admit([device, cap])
            if not guess.admitted:
                break
            wrong += 1
            good = await throttle.admit([device, cap])
            if good.admitted:
                await throttle.settle(good, clear=[device, cap])

        assert wrong == 10

    async def test_settlement_commits_so_no_bucket_lock_outlives_it(self) -> None:
        throttle, buckets, uow = _throttle()
        pair = bucket(BucketKind.PW_PAIR, "x")
        admission = await throttle.admit([pair])
        commits, flushes = uow.commit.await_count, buckets.flushes

        await throttle.settle(admission)

        assert buckets.flushes == flushes + 1
        assert uow.commit.await_count == commits + 1

    @pytest.mark.parametrize("first_to_settle", [0, 1])
    async def test_two_correct_attempts_admitted_together_settle_to_no_wait(
        self, first_to_settle: int
    ) -> None:
        """Through the throttle itself: three typos, then a double-submitted correct password.

        Both requests are admitted (the fifth charge starts a wait) and both
        settle. Afterwards the owner is not made to wait, and the shared
        budget holds exactly the three typos.
        """
        throttle, buckets, _ = _throttle()
        pair = bucket(BucketKind.PW_PAIR, "a@hospital.example", "203.0.113.7")
        account = bucket(BucketKind.PW_ACCOUNT, "a@hospital.example")
        for _ in range(3):
            assert (await throttle.admit([pair, account])).admitted

        admissions = [await throttle.admit([pair, account]) for _ in range(2)]
        assert all(admission.admitted for admission in admissions)
        assert admissions[1].wait_started == (BucketKind.PW_PAIR, 60)
        assert (await throttle.admit([pair, account])).admitted is False

        await throttle.settle(admissions[first_to_settle])
        await throttle.settle(admissions[1 - first_to_settle])

        row = buckets.rows[pair.key_hash]
        assert (row.failures, row.blocked_until) == (3, None)
        assert buckets.rows[account.key_hash].drains_at == T0 + 3 * 15 * MINUTE
        assert (await throttle.admit([pair, account])).admitted is True

    async def test_a_code_budget_of_a_verified_device_cannot_be_cleared_by_its_own_success(
        self,
    ) -> None:
        """Attack: on an MFA-verified browser, alternate wrong codes with a correct one.

        A correct code clears the device's backoff. Even a settle that names
        the device's code budget in ``clear`` leaves it a budget: ten wrong
        codes and that device is out, however many right ones come between.
        """
        throttle, _, _ = _throttle()
        origin = bucket(BucketKind.MFA_ORIGIN, "user", "device", "device-1")
        cap = bucket(BucketKind.MFA_DEVICE_CAP, "device-1")
        wrong = 0
        for _ in range(100):
            guess = await throttle.admit([cap])
            if not guess.admitted:
                break
            wrong += 1
            good = await throttle.admit([origin, cap])
            if good.admitted:
                await throttle.settle(good, clear=[origin, cap])

        assert wrong == 10
