"""Unit tests for the Patient App services — pure rules, with repositories mocked.

No database. The flows that depend on real constraints, locks and the real
throttle are in ``app/tests/api/test_patient_*`` and
``app/tests/integration/test_patient_consent_and_grants.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core.audit import AuditEvent
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.feature_flags import KNOWN_FLAGS, PATIENT_APP_ENABLED
from app.models.patient_consent import (
    ConsentPurpose,
    GranteeType,
    GrantStatus,
    PatientAccessGrant,
    RecordCategory,
)
from app.repositories.hospital_repository import HospitalSummary
from app.repositories.patient_otp_challenge_repository import OtpAttempt
from app.services.audit_service import AuditService
from app.services.patient_app import otp_service
from app.services.patient_app.access_grant_service import (
    DEFAULT_GRANT_LIFETIME,
    MAX_GRANT_LIFETIME,
    AccessGrantService,
    Grantee,
)
from app.services.patient_app.common import (
    mask_phone,
    normalize_patient_phone,
    patient_event,
    phone_reference,
)
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.errors import (
    ConsentRequiredError,
    LinkMrnRequiredError,
    LinkUnavailableError,
    OtpInvalidError,
    OtpThrottledError,
    RecordLinkRequiredError,
)
from app.services.patient_app.hospital_gate import PatientHospitalGate
from app.services.patient_app.otp_service import OTP_MAX_ATTEMPTS, OtpService
from app.services.patient_app.policies import CURRENT_POLICIES, DRAFT_VERSION, Policy
from app.tests.conftest import FakeSession, RecordingAuditSink

NOW = datetime(2026, 10, 7, 9, 0, tzinfo=UTC)


# ── Phone numbers ────────────────────────────────────────────────────────────


class TestPhone:
    @pytest.mark.parametrize(
        "typed",
        ["+919812345678", "9812345678", "09812345678", "+91 98123 45678", "+91-98123-45678"],
    )
    def test_an_indian_mobile_is_normalised_to_e164(self, typed: str) -> None:
        assert normalize_patient_phone(typed) == "+919812345678"

    @pytest.mark.parametrize(
        "typed",
        [
            "+14155550123",  # another country
            "+447911123456",
            "+915812345678",  # +91 but not a mobile range
            "+91981234567",  # too short
            "+9198123456789",  # too long
            "12345",
            "not a number",
            "",
            "+",
        ],
    )
    def test_anything_else_is_refused_with_one_message(self, typed: str) -> None:
        """Attack: have codes sent abroad (SMS pumping), or to something that is not a mobile."""
        with pytest.raises(ValidationError) as raised:
            normalize_patient_phone(typed)

        assert raised.value.message == "Enter a valid mobile number."
        assert raised.value.status_code == 422

    def test_another_country_is_accepted_only_once_it_is_allowed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "PATIENT_OTP_ALLOWED_COUNTRY_CODES", ["+91", "+971"])

        assert normalize_patient_phone("+971501234567") == "+971501234567"

    def test_the_mask_shows_the_last_four_digits_only(self) -> None:
        masked = mask_phone("+919812345678")

        assert masked.endswith("5678")
        assert "98123" not in masked

    def test_the_reference_is_stable_keyed_and_not_the_number(self) -> None:
        reference = phone_reference("+919812345678")

        assert reference == phone_reference("+919812345678")
        assert reference != phone_reference("+919812345679")
        assert len(reference) == 16
        assert "9812345678" not in reference

    def test_the_reference_changes_with_the_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A plain hash of a phone number can be reversed by trying every number."""
        from pydantic import SecretStr

        before = phone_reference("+919812345678")
        monkeypatch.setattr(settings, "PATIENT_OTP_SECRET", SecretStr("x" * 40))

        assert phone_reference("+919812345678") != before


# ── Errors ───────────────────────────────────────────────────────────────────


class TestErrors:
    def test_each_error_has_its_code_and_status(self) -> None:
        cases = {
            OtpInvalidError: (401, "OTP_INVALID"),
            OtpThrottledError: (429, "OTP_THROTTLED"),
            LinkMrnRequiredError: (409, "LINK_MRN_REQUIRED"),
            LinkUnavailableError: (403, "LINK_UNAVAILABLE"),
            ConsentRequiredError: (403, "CONSENT_REQUIRED"),
            RecordLinkRequiredError: (403, "RECORD_LINK_REQUIRED"),
        }
        for error_type, (status, code) in cases.items():
            error = error_type()
            assert (error.status_code, str(error.error_code)) == (status, code)

    def test_a_throttled_code_request_always_says_retry_after_600(self) -> None:
        assert OtpThrottledError().headers == {"Retry-After": "600"}

    def test_the_catalogue_maps_each_new_code(self) -> None:
        from app.core.error_codes import ErrorCode, http_status_for

        assert http_status_for(ErrorCode.OTP_INVALID) == 401
        assert http_status_for(ErrorCode.OTP_THROTTLED) == 429
        assert http_status_for(ErrorCode.LINK_MRN_REQUIRED) == 409
        assert http_status_for(ErrorCode.LINK_UNAVAILABLE) == 403
        assert http_status_for(ErrorCode.CONSENT_REQUIRED) == 403
        assert http_status_for(ErrorCode.RECORD_LINK_REQUIRED) == 403


# ── One-time codes ───────────────────────────────────────────────────────────


class TestOtp:
    async def test_a_code_is_six_digits_and_only_its_keyed_hash_is_stored(self) -> None:
        challenges = AsyncMock()
        issued = await OtpService(challenges).issue("+919812345678", now=NOW, ip_address=None)

        stored = challenges.create.await_args.kwargs
        assert len(issued.code) == 6
        assert issued.code.isdigit()
        assert issued.expires_in == 300
        assert stored["expires_at"] == NOW + timedelta(minutes=5)
        assert stored["challenge_id"] == issued.challenge_id
        assert len(stored["code_hash"]) == 64
        assert issued.code not in stored["code_hash"]
        assert issued.code not in repr(issued)

    def test_the_hash_is_bound_to_the_challenge_the_phone_and_the_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: replay a code against another challenge, or another number."""
        from pydantic import SecretStr

        challenge, phone, code = uuid.uuid4(), "+919812345678", "123456"
        reference = otp_service._hash_code(challenge, phone, code)  # noqa: SLF001

        assert otp_service._hash_code(challenge, phone, code) == reference  # noqa: SLF001
        assert otp_service._hash_code(uuid.uuid4(), phone, code) != reference  # noqa: SLF001
        assert otp_service._hash_code(challenge, "+919812345679", code) != reference  # noqa: SLF001
        assert otp_service._hash_code(challenge, phone, "123457") != reference  # noqa: SLF001
        monkeypatch.setattr(settings, "PATIENT_OTP_SECRET", SecretStr("y" * 40))
        assert otp_service._hash_code(challenge, phone, code) != reference  # noqa: SLF001

    async def test_the_right_code_is_correct_and_a_wrong_one_is_wrong(self) -> None:
        challenge, phone = uuid.uuid4(), "+919812345678"
        code_hash = otp_service._hash_code(challenge, phone, "246810")  # noqa: SLF001
        challenges = AsyncMock()
        challenges.register_attempt.return_value = OtpAttempt(challenge, phone, code_hash, 1)
        service = OtpService(challenges)

        right = await service.check(challenge, "246810", now=NOW)
        wrong = await service.check(challenge, "246811", now=NOW)

        assert (right.outcome, right.phone, right.attempts) == ("correct", phone, 1)
        assert wrong.outcome == "wrong"
        assert (
            challenges.register_attempt.await_args.kwargs["max_attempts"] == OTP_MAX_ATTEMPTS == 5
        )

    async def test_a_dead_challenge_reveals_nothing(self) -> None:
        challenges = AsyncMock()
        challenges.register_attempt.return_value = None

        check = await OtpService(challenges).check(uuid.uuid4(), "000000", now=NOW)

        assert check.outcome == "dead"
        assert check.phone is None
        assert check.challenge_id is None


# ── Hospital gate ────────────────────────────────────────────────────────────


def _hospital(**flags: Any) -> HospitalSummary:
    return HospitalSummary(id=uuid.uuid4(), name="City Hospital", slug="city", settings=flags)


class TestHospitalGate:
    def test_the_flag_is_not_reported_by_the_staff_capability_read(self) -> None:
        assert PATIENT_APP_ENABLED == "feature.patient_app.enabled"
        assert PATIENT_APP_ENABLED not in KNOWN_FLAGS

    async def test_an_enabled_hospital_resolves_by_id_and_by_code(self) -> None:
        hospital = _hospital(**{PATIENT_APP_ENABLED: True})
        hospitals = AsyncMock()
        hospitals.get_active_summary.return_value = hospital
        gate = PatientHospitalGate(hospitals)

        assert await gate.resolve(str(hospital.id)) is hospital
        assert hospitals.get_active_summary.await_args.kwargs == {"id": hospital.id}
        assert await gate.resolve("  City  ") is hospital
        assert hospitals.get_active_summary.await_args.kwargs == {"slug": "city"}

    @pytest.mark.parametrize(
        "flags",
        [{}, {PATIENT_APP_ENABLED: False}, {PATIENT_APP_ENABLED: "true"}, {PATIENT_APP_ENABLED: 1}],
    )
    async def test_only_an_exact_true_opens_a_hospital(self, flags: dict[str, Any]) -> None:
        hospitals = AsyncMock()
        hospitals.get_active_summary.return_value = _hospital(**flags)
        gate = PatientHospitalGate(hospitals)

        assert await gate.resolve("city") is None
        assert await gate.get_enabled(uuid.uuid4()) is None

    async def test_an_unknown_or_inactive_hospital_is_closed(self) -> None:
        hospitals = AsyncMock()
        hospitals.get_active_summary.return_value = None

        assert await PatientHospitalGate(hospitals).resolve("city") is None

    @pytest.mark.parametrize(
        "reference", ["", "   ", "../etc", "a b", "city;drop", "A" * 101, "%", "-leading"]
    )
    async def test_a_malformed_reference_never_reaches_the_database(self, reference: str) -> None:
        hospitals = AsyncMock()

        assert await PatientHospitalGate(hospitals).resolve(reference) is None
        hospitals.get_active_summary.assert_not_awaited()


# ── Consent ──────────────────────────────────────────────────────────────────


def _consent(consents: AsyncMock, *, policies: Any = CURRENT_POLICIES) -> ConsentService:
    return ConsentService(
        consents, AsyncMock(), AsyncMock(), RecordingAuditSink(), policies=policies
    )


class TestPolicies:
    def test_the_hospital_policies_are_at_the_draft_version(self) -> None:
        versions = {policy.purpose: policy for policy in CURRENT_POLICIES}

        assert DRAFT_VERSION == "2026-10-draft"
        assert versions[ConsentPurpose.HOSPITAL_RECORD_LINK].version == DRAFT_VERSION
        assert versions[ConsentPurpose.HOSPITAL_REGISTRATION].version == DRAFT_VERSION
        assert versions[ConsentPurpose.HOSPITAL_RECORD_LINK].scope == "hospital"

    def test_the_platform_policies_are_defined_but_not_yet_required(self) -> None:
        platform = [policy for policy in CURRENT_POLICIES if policy.scope == "platform"]

        assert {policy.purpose for policy in platform} == {
            ConsentPurpose.TERMS_OF_SERVICE,
            ConsentPurpose.PRIVACY_NOTICE,
        }
        assert not any(policy.required for policy in platform)

    async def test_nothing_is_pending_today_and_no_query_is_made(self) -> None:
        consents = AsyncMock()

        assert await _consent(consents).pending_policies(uuid.uuid4()) == []
        consents.get_active.assert_not_awaited()

    async def test_a_required_platform_policy_gates_the_account_until_accepted(self) -> None:
        """The gate, with a required policy injected."""
        policies = (
            Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),
            Policy(ConsentPurpose.PRIVACY_NOTICE, "2027-01", "platform", required=False),
        )
        consents = AsyncMock()
        consents.get_active.return_value = None
        service = _consent(consents, policies=policies)
        account_id = uuid.uuid4()

        pending = await service.pending_policies(account_id)

        assert [(p.purpose, p.version) for p in pending] == [("terms_of_service", "2027-01")]
        with pytest.raises(ConsentRequiredError):
            await service.ensure_policies_accepted(account_id)
        # Platform consents are looked up under no hospital, at the exact version.
        assert consents.get_active.await_args.args == (None,)
        assert consents.get_active.await_args.kwargs["policy_version"] == "2027-01"

        consents.get_active.return_value = SimpleNamespace(id=uuid.uuid4())
        assert await service.pending_policies(account_id) == []
        await service.ensure_policies_accepted(account_id)

    def test_a_stale_version_is_refused(self) -> None:
        service = _consent(AsyncMock())

        service.require_current_version([ConsentPurpose.HOSPITAL_RECORD_LINK], DRAFT_VERSION)
        with pytest.raises(ConflictError):
            service.require_current_version([ConsentPurpose.HOSPITAL_RECORD_LINK], "2025-01")

    async def test_a_hospital_purpose_cannot_be_recorded_without_its_hospital(self) -> None:
        consents = AsyncMock()
        service = _consent(consents)

        with pytest.raises(ValidationError):
            await service.record(
                uuid.uuid4(),
                ConsentPurpose.HOSPITAL_RECORD_LINK,
                hospital_id=None,
                policy_version=DRAFT_VERSION,
            )
        with pytest.raises(ValidationError):
            await service.record(
                uuid.uuid4(),
                ConsentPurpose.TERMS_OF_SERVICE,
                hospital_id=uuid.uuid4(),
                policy_version=DRAFT_VERSION,
            )
        consents.add.assert_not_awaited()


# ── Access grants: what one grant covers ─────────────────────────────────────


def _grant(**overrides: Any) -> PatientAccessGrant:
    grantee_hospital = overrides.pop("grantee_hospital_id", uuid.uuid4())
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": uuid.uuid4(),
        "patient_id": uuid.uuid4(),
        "grantor_account_id": uuid.uuid4(),
        "grantee_type": GranteeType.HOSPITAL.value,
        "grantee_id": grantee_hospital,
        "grantee_hospital_id": grantee_hospital,
        "purpose_note": "consultation",
        "categories": [RecordCategory.PRESCRIPTIONS],
        "records_from": None,
        "records_to": None,
        "granted_at": NOW - timedelta(days=1),
        "expires_at": NOW + timedelta(days=29),
        "revoked_at": None,
    }
    values.update(overrides)
    return PatientAccessGrant(**values)


def _permits(grant: PatientAccessGrant, grantee: Grantee, **kwargs: Any) -> bool:
    category = kwargs.get("category", RecordCategory.PRESCRIPTIONS)
    record_date = kwargs.get("record_date")
    now = kwargs.get("now", NOW)
    return AccessGrantService._permits(grant, grantee, category, record_date, now)  # noqa: SLF001


class TestGrantStatus:
    def test_status_is_derived_from_the_timestamps(self) -> None:
        grant = _grant()

        assert grant.status_at(NOW) is GrantStatus.ACTIVE
        assert grant.status_at(grant.expires_at) is GrantStatus.EXPIRED
        assert grant.status_at(grant.expires_at - timedelta(microseconds=1)) is GrantStatus.ACTIVE
        grant.revoked_at = NOW
        assert grant.status_at(NOW) is GrantStatus.REVOKED
        # Revoked wins over expired.
        assert grant.status_at(grant.expires_at + timedelta(days=1)) is GrantStatus.REVOKED

    def test_no_status_column_exists(self) -> None:
        assert "status" not in PatientAccessGrant.__table__.c


class TestWhatAGrantCovers:
    def test_a_hospital_grant_covers_staff_of_that_hospital(self) -> None:
        grant = _grant()

        assert _permits(grant, Grantee(hospital_id=grant.grantee_hospital_id))

    def test_another_hospitals_staff_are_not_covered(self) -> None:
        assert not _permits(_grant(), Grantee(hospital_id=uuid.uuid4()))

    def test_a_revoked_or_expired_grant_covers_nothing(self) -> None:
        revoked = _grant(revoked_at=NOW - timedelta(seconds=1))
        expired = _grant(expires_at=NOW)

        assert not _permits(revoked, Grantee(hospital_id=revoked.grantee_hospital_id))
        assert not _permits(expired, Grantee(hospital_id=expired.grantee_hospital_id))

    def test_no_category_implies_another(self) -> None:
        grant = _grant(categories=[RecordCategory.APPOINTMENTS])
        grantee = Grantee(hospital_id=grant.grantee_hospital_id)

        assert _permits(grant, grantee, category=RecordCategory.APPOINTMENTS)
        for other in RecordCategory:
            if other is not RecordCategory.APPOINTMENTS:
                assert not _permits(grant, grantee, category=other)

    def test_a_doctor_grant_covers_that_doctor_only(self) -> None:
        doctor_id = uuid.uuid4()
        grant = _grant(grantee_type=GranteeType.DOCTOR.value, grantee_id=doctor_id)
        hospital_id = grant.grantee_hospital_id

        assert _permits(grant, Grantee(hospital_id=hospital_id, doctor_id=doctor_id))
        assert not _permits(grant, Grantee(hospital_id=hospital_id, doctor_id=uuid.uuid4()))
        # A colleague who is not a doctor at all, at the same hospital.
        assert not _permits(grant, Grantee(hospital_id=hospital_id))
        # The same doctor id presented from another hospital.
        assert not _permits(grant, Grantee(hospital_id=uuid.uuid4(), doctor_id=doctor_id))

    def test_a_hospital_grant_whose_recipient_is_not_its_hospital_covers_nobody(self) -> None:
        grant = _grant()
        grant.grantee_id = uuid.uuid4()

        assert not _permits(grant, Grantee(hospital_id=grant.grantee_hospital_id))

    def test_an_unknown_grantee_type_covers_nobody(self) -> None:
        grant = _grant(grantee_type="link")

        assert not _permits(grant, Grantee(hospital_id=grant.grantee_hospital_id))

    def test_the_record_window_is_inclusive_and_enforced(self) -> None:
        grant = _grant(records_from=date(2026, 1, 1), records_to=date(2026, 6, 30))
        grantee = Grantee(hospital_id=grant.grantee_hospital_id)

        assert _permits(grant, grantee, record_date=date(2026, 1, 1))
        assert _permits(grant, grantee, record_date=date(2026, 6, 30))
        assert not _permits(grant, grantee, record_date=date(2025, 12, 31))
        assert not _permits(grant, grantee, record_date=date(2026, 7, 1))
        # A windowed grant never covers a record with no date.
        assert not _permits(grant, grantee, record_date=None)

    def test_a_half_open_window_bounds_one_side_only(self) -> None:
        since = _grant(records_from=date(2026, 1, 1))
        until = _grant(records_to=date(2026, 1, 1))

        assert _permits(since, Grantee(since.grantee_hospital_id), record_date=date(2030, 1, 1))
        assert not _permits(since, Grantee(since.grantee_hospital_id), record_date=date(2025, 1, 1))
        assert _permits(until, Grantee(until.grantee_hospital_id), record_date=date(2020, 1, 1))
        assert not _permits(until, Grantee(until.grantee_hospital_id), record_date=date(2026, 1, 2))


class TestGrantValidation:
    def test_documents_cannot_be_selected(self) -> None:
        with pytest.raises(ValidationError):
            AccessGrantService._validated_categories(  # noqa: SLF001
                [RecordCategory.PRESCRIPTIONS, RecordCategory.DOCUMENTS]
            )

    def test_at_least_one_known_category_is_needed(self) -> None:
        with pytest.raises(ValidationError):
            AccessGrantService._validated_categories([])  # noqa: SLF001
        with pytest.raises(ValidationError):
            AccessGrantService._validated_categories(["everything"])  # type: ignore[list-item]  # noqa: SLF001

    def test_duplicates_collapse(self) -> None:
        chosen = AccessGrantService._validated_categories(  # noqa: SLF001
            [RecordCategory.IDENTITY, RecordCategory.IDENTITY, RecordCategory.LAB_RESULTS]
        )

        assert chosen == [RecordCategory.IDENTITY, RecordCategory.LAB_RESULTS]

    def test_expiry_defaults_to_thirty_days_and_is_capped_at_a_year(self) -> None:
        validated = AccessGrantService._validated_expiry  # noqa: SLF001

        assert timedelta(days=30) == DEFAULT_GRANT_LIFETIME
        assert timedelta(days=365) == MAX_GRANT_LIFETIME
        assert validated(None, NOW) == NOW + timedelta(days=30)
        assert validated(NOW + timedelta(days=365), NOW) == NOW + timedelta(days=365)
        with pytest.raises(ValidationError):
            validated(NOW + timedelta(days=365, seconds=1), NOW)
        with pytest.raises(ValidationError):
            validated(NOW, NOW)
        with pytest.raises(ValidationError):
            validated(datetime(2026, 12, 1), NOW)  # noqa: DTZ001 — naive on purpose

    async def test_the_documents_category_can_never_be_read_on_a_grant(self) -> None:
        grants = AsyncMock()
        service = AccessGrantService(
            grants, AsyncMock(), AsyncMock(), AsyncMock(), uow=AsyncMock(), audit=AsyncMock()
        )

        with pytest.raises(NotFoundError):
            await service.authorize(
                Grantee(uuid.uuid4()), uuid.uuid4(), uuid.uuid4(), RecordCategory.DOCUMENTS, None
            )
        grants.list_unrevoked_for_recipient.assert_not_awaited()


# ── Audit: the patient actor ─────────────────────────────────────────────────


class TestPatientAuditActor:
    def test_a_patient_event_never_carries_a_staff_actor(self) -> None:
        account_id = uuid.uuid4()

        event = patient_event(
            "patient.auth.login", target_type="patient_account", account_id=account_id
        )

        assert event.actor_type == "patient"
        assert event.patient_account_id == account_id
        assert event.actor_id is None
        assert event.hospital_id is None

    def test_a_staff_event_is_unchanged_by_default(self) -> None:
        event = AuditEvent(
            action="patient.created", hospital_id=uuid.uuid4(), target_type="patient"
        )

        assert event.actor_type == "staff"
        assert event.patient_account_id is None
        assert event.ip_address is None

    async def test_a_patient_event_is_stored_as_a_patient_not_as_the_system(self) -> None:
        """Before S1, an event with no user id was recorded as ``system``."""
        repo = AsyncMock()
        account_id = uuid.uuid4()
        service = AuditService(FakeSession(), repo, AsyncMock())  # type: ignore[arg-type]

        await service.record(
            patient_event(
                "patient.auth.login",
                target_type="patient_account",
                account_id=account_id,
                context={"phone_ref": "abc"},
            )
        )

        stored = repo.add_entry.await_args.kwargs
        assert stored["actor_type"] == "patient"
        assert stored["patient_account_id"] == account_id
        assert stored["actor_user_id"] is None
        assert stored["hospital_id"] is None

    @pytest.mark.parametrize(("actor_id", "expected"), [(uuid.uuid4(), "user"), (None, "system")])
    async def test_staff_and_system_events_are_stored_exactly_as_before(
        self, actor_id: uuid.UUID | None, expected: str
    ) -> None:
        repo = AsyncMock()
        service = AuditService(FakeSession(), repo, AsyncMock())  # type: ignore[arg-type]

        await service.record(
            AuditEvent(
                action="patient.created",
                hospital_id=uuid.uuid4(),
                target_type="patient",
                actor_id=actor_id,
            )
        )

        stored = repo.add_entry.await_args.kwargs
        assert stored["actor_type"] == expected
        assert stored["actor_user_id"] == actor_id
        assert stored["patient_account_id"] is None
        assert stored["ip_address"] is None

    async def test_a_staff_id_on_a_patient_event_is_never_stored_as_the_actor(self) -> None:
        repo = AsyncMock()
        service = AuditService(FakeSession(), repo, AsyncMock())  # type: ignore[arg-type]
        event = AuditEvent(
            action="patient.auth.login",
            hospital_id=None,
            target_type="patient_account",
            actor_id=uuid.uuid4(),
            actor_type="patient",
            patient_account_id=uuid.uuid4(),
        )

        await service.record(event)

        assert repo.add_entry.await_args.kwargs["actor_user_id"] is None
