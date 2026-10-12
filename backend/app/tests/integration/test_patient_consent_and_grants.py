"""Consent, patient authorization and record access grants, against a real PostgreSQL.

``docs/modules/15-patient-app.md`` §5.7, §6, §7 and §8. None of this has an
HTTP caller yet, so the services are driven directly — which is also how the
reading side will call :meth:`AccessGrantService.authorize` when it exists.

Each test builds the services on the test's rolled-back session, exactly as
the dependency providers compose them.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import (
    TenantScope,
    TenantScopeRequiredError,
    cross_tenant,
    current_tenant_scope,
    tenant_scope,
)
from app.database.unit_of_work import UnitOfWork
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.patient_account import PatientAccount, PatientAccountLink
from app.models.patient_consent import (
    ConsentPurpose,
    GranteeType,
    GrantPurposeNote,
    GrantStatus,
    PatientAccessGrant,
    PatientConsentRecord,
    RecordCategory,
)
from app.repositories import (
    AuditLogRepository,
    DoctorRepository,
    HospitalRepository,
    MrnSequenceRepository,
    PatientAccessGrantRepository,
    PatientAccountLinkRepository,
    PatientAccountRepository,
    PatientConsentRepository,
    PatientRepository,
    UserRepository,
)
from app.services.audit_service import AuditService
from app.services.mrn_service import MRNService
from app.services.patient_app.access_grant_service import AccessGrantService, Grantee
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.hospital_gate import PatientHospitalGate
from app.services.patient_app.patient_authorization import PatientAuthorization, PatientContext
from app.services.patient_app.policies import CURRENT_POLICIES, DRAFT_VERSION, Policy
from app.services.patient_service import PatientService
from app.tests.billing_helpers import insert_doctor
from app.tests.patient_app_helpers import (
    audit_rows,
    insert_patient_record,
    new_phone,
    open_hospital,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

TODAY = date(2026, 10, 7)


@dataclass
class _Services:
    consent: ConsentService
    authorization: PatientAuthorization
    grants: AccessGrantService
    links: PatientAccountLinkRepository
    consents: PatientConsentRepository
    grant_rows: PatientAccessGrantRepository


def _services(session: AsyncSession, policies: Sequence[Policy] = CURRENT_POLICIES) -> _Services:
    """Compose the patient services the way ``app/api/dependencies/patient.py`` does."""
    uow = UnitOfWork(session)
    audit = AuditService(session, AuditLogRepository(session), UserRepository(session))
    links = PatientAccountLinkRepository(session)
    consents = PatientConsentRepository(session)
    hospitals = HospitalRepository(session)
    consent = ConsentService(consents, links, uow, audit, policies=policies)
    patients = PatientService(
        PatientRepository(session), MRNService(MrnSequenceRepository(session)), session, audit
    )
    authorization = PatientAuthorization(
        PatientAccountRepository(session), links, PatientHospitalGate(hospitals), patients, consent
    )
    grant_rows = PatientAccessGrantRepository(session)
    grants = AccessGrantService(
        grant_rows, authorization, hospitals, DoctorRepository(session), uow=uow, audit=audit
    )
    return _Services(consent, authorization, grants, links, consents, grant_rows)


@dataclass
class _Linked:
    """An account linked to its own record at a hospital."""

    account: PatientAccount
    patient: Patient
    link: PatientAccountLink
    hospital_id: uuid.UUID


async def _account(session: AsyncSession, phone: str | None = None) -> PatientAccount:
    account = PatientAccount(id=uuid.uuid4(), phone=phone or new_phone(), status="active")
    session.add(account)
    await session.flush()
    await session.commit()
    return account


async def _linked(
    session: AsyncSession, hospital_id: uuid.UUID, *, phone: str | None = None
) -> _Linked:
    """Sign-up, link and consent as the link endpoint leaves them."""
    account = await _account(session, phone)
    patient = await insert_patient_record(session, hospital_id, phone=account.phone)
    services = _services(session)
    link = await services.links.create_link(
        hospital_id,
        account_id=account.id,
        patient_id=patient.id,
        verified_via="phone_dob",
        now=datetime.now(UTC),
    )
    await services.consent.record(
        account.id,
        ConsentPurpose.HOSPITAL_RECORD_LINK,
        hospital_id=hospital_id,
        policy_version=DRAFT_VERSION,
    )
    await session.commit()
    return _Linked(account, patient, link, hospital_id)


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    return await open_hospital(db_session, hospital_id)


@pytest_asyncio.fixture
async def other_hospital(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> Hospital:
    return await open_hospital(db_session, other_hospital_id)


@pytest_asyncio.fixture
async def linked(db_session: AsyncSession, hospital: Hospital) -> _Linked:
    return await _linked(db_session, hospital.id)


# ── Consent ──────────────────────────────────────────────────────────────────


class TestConsent:
    async def test_a_consent_is_recorded_with_its_evidence_and_audited(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        from app.services.patient_app.common import ClientContext

        account = await _account(db_session)
        services = _services(db_session)

        record = await services.consent.accept(
            account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            policy_version=DRAFT_VERSION,
            hospital_id=hospital.id,
            client=ClientContext(ip_address="203.0.113.9", user_agent="pytest"),
        )

        assert record.hospital_id == hospital.id
        assert record.policy_version == DRAFT_VERSION
        assert str(record.ip_address) == "203.0.113.9"
        assert record.user_agent == "pytest"
        [event] = await audit_rows(db_session, "patient.consent.granted")
        assert event.actor_type == "patient"
        assert event.patient_account_id == account.id
        assert event.hospital_id == hospital.id
        assert event.context == {"purpose": "hospital_record_link", "policy_version": DRAFT_VERSION}
        assert str(event.ip_address) == "203.0.113.9"

    async def test_recording_the_same_consent_twice_adds_nothing(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        services = _services(db_session)

        again = await services.consent.accept(
            linked.account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            policy_version=DRAFT_VERSION,
            hospital_id=linked.hospital_id,
        )

        rows = (await db_session.execute(select(PatientConsentRecord))).scalars().all()
        assert [row.id for row in rows] == [again.id]
        assert len(await audit_rows(db_session, "patient.consent.granted")) == 1

    async def test_a_stale_version_is_never_recorded(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        account = await _account(db_session)

        with pytest.raises(ConflictError):
            await _services(db_session).consent.accept(
                account.id,
                ConsentPurpose.HOSPITAL_RECORD_LINK,
                policy_version="2025-01",
                hospital_id=hospital.id,
            )

        assert (await db_session.execute(select(PatientConsentRecord))).scalars().all() == []

    async def test_a_new_policy_version_asks_again_and_keeps_the_old_row(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        newer = (Policy(ConsentPurpose.HOSPITAL_RECORD_LINK, "2027-01", "hospital", required=True),)
        services = _services(db_session, newer)

        assert not await services.consent.is_in_force(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )
        await services.consent.accept(
            linked.account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            policy_version="2027-01",
            hospital_id=linked.hospital_id,
        )

        rows = (await db_session.execute(select(PatientConsentRecord))).scalars().all()
        assert sorted(row.policy_version for row in rows) == sorted([DRAFT_VERSION, "2027-01"])
        assert await services.consent.is_in_force(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )

    async def test_withdrawing_the_record_link_consent_ends_the_link(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        services = _services(db_session)

        ended = await services.consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )

        assert ended == 1
        link = (
            await db_session.execute(
                select(PatientAccountLink).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert link.unlinked_at is not None
        assert link.unlink_reason == "consent_withdrawn"
        [record] = (await db_session.execute(select(PatientConsentRecord))).scalars().all()
        assert record.withdrawn_at is not None  # kept as evidence, never deleted
        assert len(await audit_rows(db_session, "patient.consent.withdrawn")) == 1
        [link_ended] = await audit_rows(db_session, "patient.link.ended")
        assert link_ended.context == {"reason": "consent_withdrawn"}
        with pytest.raises(NotFoundError):
            await services.authorization.resolve_context(linked.account, linked.hospital_id)

    async def test_withdrawing_at_one_hospital_leaves_the_other_alone(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        here = await _linked(db_session, hospital.id)
        patient_there = await insert_patient_record(
            db_session, other_hospital.id, phone=here.account.phone
        )
        services = _services(db_session)
        await services.links.create_link(
            other_hospital.id,
            account_id=here.account.id,
            patient_id=patient_there.id,
            verified_via="phone_dob",
            now=datetime.now(UTC),
        )
        await services.consent.accept(
            here.account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            policy_version=DRAFT_VERSION,
            hospital_id=other_hospital.id,
        )

        await services.consent.withdraw(
            here.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=hospital.id
        )

        context = await services.authorization.resolve_context(here.account, other_hospital.id)
        assert context.patient_id == patient_there.id

    async def test_a_required_platform_policy_is_pending_until_accepted(
        self, db_session: AsyncSession
    ) -> None:
        """The gate, end to end, with a required policy injected."""
        required = (
            Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),
            *CURRENT_POLICIES[1:],
        )
        account = await _account(db_session)
        services = _services(db_session, required)

        assert [p.purpose for p in await services.consent.pending_policies(account.id)] == [
            "terms_of_service"
        ]
        with pytest.raises(ConsentRequiredError):
            await services.consent.ensure_policies_accepted(account.id)

        record = await services.consent.accept(
            account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01"
        )

        assert record.hospital_id is None
        assert await services.consent.pending_policies(account.id) == []
        await services.consent.ensure_policies_accepted(account.id)

        await services.consent.withdraw(account.id, ConsentPurpose.TERMS_OF_SERVICE)
        with pytest.raises(ConsentRequiredError):
            await services.consent.ensure_policies_accepted(account.id)

    async def test_one_accounts_acceptance_is_not_anothers(self, db_session: AsyncSession) -> None:
        required = (Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True),)
        accepted, pending = await _account(db_session), await _account(db_session)
        services = _services(db_session, required)

        await services.consent.accept(
            accepted.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01"
        )

        assert await services.consent.pending_policies(accepted.id) == []
        assert len(await services.consent.pending_policies(pending.id)) == 1


# ── Patient authorization: the one entry point ───────────────────────────────


class TestPatientAuthorization:
    async def test_a_linked_account_resolves_to_its_own_record(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        context = await _services(db_session).authorization.resolve_context(
            linked.account, linked.hospital_id
        )

        assert context == PatientContext(
            account_id=linked.account.id,
            hospital_id=linked.hospital_id,
            patient_id=linked.patient.id,
        )

    async def test_resolving_leaves_no_tenant_scope_bound(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        await _services(db_session).authorization.resolve_context(
            linked.account, linked.hospital_id
        )

        assert current_tenant_scope() is None

    async def test_an_account_with_no_link_gets_a_404(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        stranger = await _account(db_session)
        await insert_patient_record(db_session, hospital.id, phone=stranger.phone)

        with pytest.raises(NotFoundError):
            await _services(db_session).authorization.resolve_context(stranger, hospital.id)

    async def test_a_link_at_hospital_a_gives_nothing_at_hospital_b(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Even when hospital B also holds a record on the same phone."""
        await insert_patient_record(db_session, other_hospital.id, phone=linked.account.phone)

        with pytest.raises(NotFoundError):
            await _services(db_session).authorization.resolve_context(
                linked.account, other_hospital.id
            )

    async def test_another_accounts_link_is_never_resolved(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        mine = await _linked(db_session, hospital.id)
        theirs = await _linked(db_session, hospital.id)

        context = await _services(db_session).authorization.resolve_context(
            mine.account, hospital.id
        )

        assert context.patient_id == mine.patient.id
        assert context.patient_id != theirs.patient.id

    @pytest.mark.parametrize("change", ["phone", "deactivated", "flag_off", "hospital_inactive"])
    async def test_a_link_that_is_not_honoured_right_now_is_a_404(
        self, db_session: AsyncSession, linked: _Linked, change: str
    ) -> None:
        if change == "phone":
            await db_session.execute(
                update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
            )
        elif change == "deactivated":
            await db_session.execute(
                update(Patient)
                .where(Patient.id == linked.patient.id)
                .values(deleted_at=datetime.now(UTC))
            )
        elif change == "flag_off":
            await open_hospital(db_session, linked.hospital_id, enabled=False)
        else:
            await db_session.execute(
                update(Hospital).where(Hospital.id == linked.hospital_id).values(is_active=False)
            )
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await _services(db_session).authorization.resolve_context(
                linked.account, linked.hospital_id
            )

    async def test_the_link_is_honoured_again_when_the_phone_is_put_back(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        """Read-time evaluation: nothing was stored, so nothing has to be undone."""
        for phone in (new_phone(), linked.account.phone):
            await db_session.execute(
                update(Patient).where(Patient.id == linked.patient.id).values(phone=phone)
            )
            await db_session.commit()

        context = await _services(db_session).authorization.resolve_context(
            linked.account, linked.hospital_id
        )
        assert context.patient_id == linked.patient.id

    async def test_without_the_record_link_consent_it_is_consent_required(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        await db_session.execute(
            update(PatientConsentRecord).values(withdrawn_at=datetime.now(UTC))
        )
        await db_session.commit()

        with pytest.raises(ConsentRequiredError):
            await _services(db_session).authorization.resolve_context(
                linked.account, linked.hospital_id
            )

    async def test_a_pending_platform_policy_blocks_before_anything_else(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        required = (
            Policy(ConsentPurpose.PRIVACY_NOTICE, "2027-01", "platform", required=True),
            *(p for p in CURRENT_POLICIES if p.purpose is not ConsentPurpose.PRIVACY_NOTICE),
        )

        with pytest.raises(ConsentRequiredError):
            await _services(db_session, required).authorization.resolve_context(
                linked.account, uuid.uuid4()
            )

    async def test_describe_links_marks_a_broken_binding_as_suspended(
        self, db_session: AsyncSession, linked: _Linked, hospital: Hospital
    ) -> None:
        services = _services(db_session)
        [healthy] = await services.authorization.describe_links(linked.account)
        await db_session.execute(
            update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
        )
        await db_session.commit()

        [broken] = await services.authorization.describe_links(linked.account)

        assert (healthy.hospital_id, healthy.hospital_name) == (hospital.id, hospital.name)
        assert (healthy.suspended, broken.suspended) == (False, True)


# ── Repository tenancy ───────────────────────────────────────────────────────


class TestRepositoryTenancy:
    async def test_link_lookups_are_confined_to_the_hospital_given(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        links = PatientAccountLinkRepository(db_session)

        assert await links.get_active_self(linked.hospital_id, linked.account.id) is not None
        assert await links.get_active_self(other_hospital.id, linked.account.id) is None
        assert await links.get_active_for_patient(linked.hospital_id, linked.patient.id) is not None
        assert await links.get_active_for_patient(other_hospital.id, linked.patient.id) is None

    async def test_listing_an_accounts_links_needs_a_scope(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        links = PatientAccountLinkRepository(db_session)

        own = await links.list_active_for_account(linked.account.id, cross_tenant("own links"))
        here = await links.list_active_for_account(linked.account.id, linked.hospital_id)
        there = await links.list_active_for_account(linked.account.id, other_hospital.id)

        assert [link.id for link in own] == [link.id for link in here] == [linked.link.id]
        assert there == []
        with pytest.raises(TenantScopeRequiredError):
            await links.list_active_for_account(linked.account.id, None)  # type: ignore[arg-type]

    async def test_the_ambient_scope_hides_another_hospitals_links(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Layer 2: under hospital B's scope, a query that names hospital A finds nothing."""
        links = PatientAccountLinkRepository(db_session)

        with tenant_scope(TenantScope.hospital(other_hospital.id)):
            assert await links.get_active_self(linked.hospital_id, linked.account.id) is None
            assert (
                await links.list_active_for_account(linked.account.id, cross_tenant("own links"))
                == []
            )

    async def test_the_phone_match_is_exact_scoped_and_sees_deactivated_records(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        phone = new_phone()
        active = await insert_patient_record(db_session, hospital.id, phone=phone)
        inactive = await insert_patient_record(db_session, hospital.id, phone=phone, deleted=True)
        await insert_patient_record(db_session, hospital.id, phone=new_phone())
        await insert_patient_record(db_session, other_hospital.id, phone=phone)
        patients = PatientRepository(db_session)

        everything = await patients.list_by_phone(hospital.id, phone)
        live = await patients.list_by_phone(hospital.id, phone, include_deleted=False)

        assert {patient.id for patient in everything} == {active.id, inactive.id}
        assert [patient.id for patient in live] == [active.id]
        assert await patients.list_by_phone(hospital.id, phone[:-1]) == []

    async def test_consents_are_confined_to_their_hospital_or_to_none(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        consents = PatientConsentRepository(db_session)
        lookup: dict[str, Any] = {
            "account_id": linked.account.id,
            "purpose": "hospital_record_link",
            "policy_version": DRAFT_VERSION,
        }

        assert await consents.get_active(linked.hospital_id, **lookup) is not None
        assert await consents.get_active(other_hospital.id, **lookup) is None
        # ``None`` means "a platform consent", never "any hospital".
        assert await consents.get_active(None, **lookup) is None

    async def test_a_second_active_link_to_one_record_is_refused_by_the_database(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        intruder = await _account(db_session)

        with pytest.raises(IntegrityError):
            await PatientAccountLinkRepository(db_session).create_link(
                linked.hospital_id,
                account_id=intruder.id,
                patient_id=linked.patient.id,
                verified_via="phone_dob",
                now=datetime.now(UTC),
            )
        await db_session.rollback()


# ── Access grants ────────────────────────────────────────────────────────────


async def _grant(
    session: AsyncSession, linked: _Linked, recipient_hospital_id: uuid.UUID, **overrides: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "grantee_type": GranteeType.HOSPITAL,
        "grantee_id": recipient_hospital_id,
        "grantee_hospital_id": recipient_hospital_id,
        "categories": [RecordCategory.PRESCRIPTIONS, RecordCategory.LAB_RESULTS],
        "purpose_note": GrantPurposeNote.SECOND_OPINION,
    }
    values.update(overrides)
    view = await _services(session).grants.create(linked.account, linked.hospital_id, **values)
    return view.id


class TestCreateGrant:
    async def test_a_grant_is_created_for_thirty_days_and_audited(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        before = datetime.now(UTC)

        view = await _services(db_session).grants.create(
            linked.account,
            linked.hospital_id,
            grantee_type=GranteeType.HOSPITAL,
            grantee_id=other_hospital.id,
            grantee_hospital_id=other_hospital.id,
            categories=[RecordCategory.PRESCRIPTIONS, RecordCategory.PRESCRIPTIONS],
            purpose_note=GrantPurposeNote.CONSULTATION,
        )

        assert view.status is GrantStatus.ACTIVE
        assert view.hospital_id == linked.hospital_id
        assert view.categories == [RecordCategory.PRESCRIPTIONS]
        assert (
            timedelta(days=30) - timedelta(minutes=1)
            < view.expires_at - before
            <= timedelta(days=30, minutes=1)
        )
        row = (await db_session.execute(select(PatientAccessGrant))).scalar_one()
        assert row.patient_id == linked.patient.id  # resolved by the server, from the link
        assert row.grantor_account_id == linked.account.id
        assert row.categories == [RecordCategory.PRESCRIPTIONS]
        [event] = await audit_rows(db_session, "patient.access_grant.created")
        assert event.actor_type == "patient"
        assert event.patient_account_id == linked.account.id
        assert event.hospital_id == linked.hospital_id
        assert event.target_id == view.id

    async def test_the_grantor_needs_an_honoured_link(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        stranger = await _account(db_session)
        services = _services(db_session)
        arguments: dict[str, Any] = {
            "grantee_type": GranteeType.HOSPITAL,
            "grantee_id": other_hospital.id,
            "grantee_hospital_id": other_hospital.id,
            "categories": [RecordCategory.IDENTITY],
            "purpose_note": GrantPurposeNote.CONSULTATION,
        }

        with pytest.raises(NotFoundError):
            await services.grants.create(stranger, linked.hospital_id, **arguments)

        await db_session.execute(
            update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
        )
        await db_session.commit()
        with pytest.raises(NotFoundError):
            await services.grants.create(linked.account, linked.hospital_id, **arguments)
        assert (await db_session.execute(select(PatientAccessGrant))).scalars().all() == []

    @pytest.mark.parametrize(
        "overrides",
        [
            {"categories": []},
            {"categories": [RecordCategory.DOCUMENTS]},
            {"categories": [RecordCategory.IDENTITY, RecordCategory.DOCUMENTS]},
            {"expires_at": datetime.now(UTC) + timedelta(days=366)},
            {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
            {"records_from": date(2026, 6, 1), "records_to": date(2026, 1, 1)},
            {"context_appointment_id": uuid.uuid4()},
            {"grantee_id": uuid.uuid4()},  # a hospital grant must name its own hospital
            {"grantee_hospital_id": uuid.uuid4()},  # no such hospital
        ],
    )
    async def test_an_invalid_grant_is_refused(
        self,
        db_session: AsyncSession,
        linked: _Linked,
        other_hospital: Hospital,
        overrides: dict[str, Any],
    ) -> None:
        with pytest.raises(ValidationError):
            await _grant(db_session, linked, other_hospital.id, **overrides)

        assert (await db_session.execute(select(PatientAccessGrant))).scalars().all() == []

    async def test_a_doctor_grant_needs_a_doctor_of_that_hospital(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        doctor = await insert_doctor(db_session, other_hospital.id)
        doctor_elsewhere = await insert_doctor(db_session, linked.hospital_id)
        await db_session.commit()

        with pytest.raises(ValidationError):
            await _grant(
                db_session,
                linked,
                other_hospital.id,
                grantee_type=GranteeType.DOCTOR,
                grantee_id=doctor_elsewhere.id,
            )
        grant_id = await _grant(
            db_session,
            linked,
            other_hospital.id,
            grantee_type=GranteeType.DOCTOR,
            grantee_id=doctor.id,
        )

        row = (await db_session.execute(select(PatientAccessGrant))).scalar_one()
        assert (row.id, row.grantee_type, row.grantee_id) == (grant_id, "doctor", doctor.id)

    async def test_a_year_is_the_longest_a_grant_can_last(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        expires_at = datetime.now(UTC) + timedelta(days=364, hours=23)

        await _grant(db_session, linked, other_hospital.id, expires_at=expires_at)

        row = (await db_session.execute(select(PatientAccessGrant))).scalar_one()
        assert row.expires_at == expires_at


class TestAuthorize:
    """``AccessGrantService.authorize`` — every check of §8.3.1."""

    async def _authorize(
        self,
        session: AsyncSession,
        linked: _Linked,
        grantee: Grantee,
        *,
        category: RecordCategory = RecordCategory.PRESCRIPTIONS,
        record_date: date | None = TODAY,
        patient_id: uuid.UUID | None = None,
        source_hospital_id: uuid.UUID | None = None,
    ) -> PatientAccessGrant:
        return await _services(session).grants.authorize(
            grantee,
            patient_id or linked.patient.id,
            source_hospital_id or linked.hospital_id,
            category,
            record_date,
        )

    async def test_staff_of_the_recipient_hospital_are_authorised(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        grant_id = await _grant(db_session, linked, other_hospital.id)

        grant = await self._authorize(db_session, linked, Grantee(other_hospital.id))

        assert grant.id == grant_id

    async def test_with_no_grant_the_answer_is_a_404(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        with pytest.raises(NotFoundError):
            await self._authorize(db_session, linked, Grantee(other_hospital.id))

    async def test_another_hospital_is_not_authorised(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await _grant(db_session, linked, other_hospital.id)
        third = await open_hospital(
            db_session, (await _third_hospital(db_session)).id, enabled=True
        )

        with pytest.raises(NotFoundError):
            await self._authorize(db_session, linked, Grantee(third.id))

    async def test_a_category_that_was_not_granted_is_not_authorised(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await _grant(
            db_session, linked, other_hospital.id, categories=[RecordCategory.APPOINTMENTS]
        )
        grantee = Grantee(other_hospital.id)

        await self._authorize(db_session, linked, grantee, category=RecordCategory.APPOINTMENTS)
        for category in (
            RecordCategory.PRESCRIPTIONS,
            RecordCategory.LAB_RESULTS,
            RecordCategory.MEDICAL_HISTORY,
            RecordCategory.IDENTITY,
            RecordCategory.DOCUMENTS,
        ):
            with pytest.raises(NotFoundError):
                await self._authorize(db_session, linked, grantee, category=category)

    async def test_revocation_takes_effect_on_the_next_read(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        grant_id = await _grant(db_session, linked, other_hospital.id)
        services = _services(db_session)
        await self._authorize(db_session, linked, Grantee(other_hospital.id))

        view = await services.grants.revoke(
            linked.account, linked.hospital_id, grant_id, reason="No longer needed"
        )

        assert view.status is GrantStatus.REVOKED
        with pytest.raises(NotFoundError):
            await self._authorize(db_session, linked, Grantee(other_hospital.id))
        [event] = await audit_rows(db_session, "patient.access_grant.revoked")
        assert event.target_id == grant_id
        assert event.patient_account_id == linked.account.id
        # The row and its history are kept.
        row = (
            await db_session.execute(
                select(PatientAccessGrant).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.revoked_by_account_id == linked.account.id
        assert row.revoke_reason == "No longer needed"

    async def test_an_expired_grant_is_not_authorised(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await _grant(db_session, linked, other_hospital.id)
        past = datetime.now(UTC) - timedelta(days=40)
        await db_session.execute(
            update(PatientAccessGrant).values(granted_at=past, expires_at=past + timedelta(days=30))
        )
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await self._authorize(db_session, linked, Grantee(other_hospital.id))
        [listed] = await _services(db_session).grants.list_for_account(
            linked.account, linked.hospital_id
        )
        assert listed.status is GrantStatus.EXPIRED

    async def test_the_record_window_is_enforced(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await _grant(
            db_session,
            linked,
            other_hospital.id,
            records_from=date(2026, 1, 1),
            records_to=date(2026, 6, 30),
        )
        grantee = Grantee(other_hospital.id)

        await self._authorize(db_session, linked, grantee, record_date=date(2026, 3, 15))
        for outside in (date(2025, 12, 31), date(2026, 7, 1), None):
            with pytest.raises(NotFoundError):
                await self._authorize(db_session, linked, grantee, record_date=outside)

    async def test_a_doctor_grant_authorises_that_doctor_only(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        doctor = await insert_doctor(db_session, other_hospital.id)
        colleague = await insert_doctor(db_session, other_hospital.id)
        await db_session.commit()
        await _grant(
            db_session,
            linked,
            other_hospital.id,
            grantee_type=GranteeType.DOCTOR,
            grantee_id=doctor.id,
        )

        await self._authorize(db_session, linked, Grantee(other_hospital.id, doctor.id))
        for grantee in (
            Grantee(other_hospital.id, colleague.id),
            Grantee(other_hospital.id),
            Grantee(linked.hospital_id, doctor.id),
        ):
            with pytest.raises(NotFoundError):
                await self._authorize(db_session, linked, grantee)

    @pytest.mark.parametrize("change", ["link_ended", "phone", "deactivated", "account_suspended"])
    async def test_a_grant_is_unusable_once_the_grantors_link_is_not_live(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital, change: str
    ) -> None:
        """The grant row is untouched; it simply stops authorising anything."""
        await _grant(db_session, linked, other_hospital.id)
        if change == "link_ended":
            await db_session.execute(
                update(PatientAccountLink).values(unlinked_at=datetime.now(UTC))
            )
        elif change == "phone":
            await db_session.execute(
                update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
            )
        elif change == "deactivated":
            await db_session.execute(
                update(Patient)
                .where(Patient.id == linked.patient.id)
                .values(deleted_at=datetime.now(UTC))
            )
        else:
            await db_session.execute(
                update(PatientAccount)
                .where(PatientAccount.id == linked.account.id)
                .values(status="suspended")
            )
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await self._authorize(db_session, linked, Grantee(other_hospital.id))
        row = (
            await db_session.execute(
                select(PatientAccessGrant).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.revoked_at is None

    async def test_a_grant_on_one_record_authorises_no_other_record(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        sharer = await _linked(db_session, hospital.id)
        bystander = await _linked(db_session, hospital.id)
        await _grant(db_session, sharer, other_hospital.id)

        with pytest.raises(NotFoundError):
            await self._authorize(
                db_session, sharer, Grantee(other_hospital.id), patient_id=bystander.patient.id
            )

    async def test_the_source_hospital_must_be_the_one_that_holds_the_record(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await _grant(db_session, linked, other_hospital.id)

        with pytest.raises(NotFoundError):
            await self._authorize(
                db_session,
                linked,
                Grantee(other_hospital.id),
                source_hospital_id=other_hospital.id,
            )


async def _third_hospital(session: AsyncSession) -> Hospital:
    hospital = Hospital(
        id=uuid.uuid4(),
        name="Third Hospital",
        slug=f"third-{uuid.uuid4().hex[:10]}",
        address={"line1": "3 Test Road", "city": "Hyderabad", "country": "IN"},
        settings={},
    )
    session.add(hospital)
    await session.flush()
    await session.commit()
    return hospital


class TestRevokeAndList:
    async def test_only_the_grantor_can_revoke(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        grantor = await _linked(db_session, hospital.id)
        someone_else = await _linked(db_session, hospital.id)
        grant_id = await _grant(db_session, grantor, other_hospital.id)
        services = _services(db_session)

        with pytest.raises(NotFoundError):
            await services.grants.revoke(someone_else.account, hospital.id, grant_id)
        with pytest.raises(NotFoundError):
            await services.grants.revoke(grantor.account, other_hospital.id, grant_id)

        row = (
            await db_session.execute(
                select(PatientAccessGrant).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.revoked_at is None

    async def test_revoking_twice_changes_nothing(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        grant_id = await _grant(db_session, linked, other_hospital.id)
        services = _services(db_session)

        first = await services.grants.revoke(linked.account, linked.hospital_id, grant_id)
        second = await services.grants.revoke(linked.account, linked.hospital_id, grant_id)

        assert first.revoked_at == second.revoked_at
        assert len(await audit_rows(db_session, "patient.access_grant.revoked")) == 1

    async def test_a_grant_can_be_revoked_after_the_link_is_suspended(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """A patient can always take back what they gave."""
        grant_id = await _grant(db_session, linked, other_hospital.id)
        await db_session.execute(
            update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
        )
        await db_session.commit()

        view = await _services(db_session).grants.revoke(
            linked.account, linked.hospital_id, grant_id
        )

        assert view.status is GrantStatus.REVOKED

    async def test_an_account_lists_only_its_own_grants(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        mine = await _linked(db_session, hospital.id)
        theirs = await _linked(db_session, hospital.id)
        my_grant = await _grant(db_session, mine, other_hospital.id)
        await _grant(db_session, theirs, other_hospital.id)
        services = _services(db_session)

        listed = await services.grants.list_for_account(mine.account, hospital.id)

        assert [grant.id for grant in listed] == [my_grant]
        assert await services.grants.list_for_account(mine.account, other_hospital.id) == []


# ── Demo seed ────────────────────────────────────────────────────────────────


class TestDemoSeedFlag:
    """The demo hospital is opened to the Patient App unless the flag was set explicitly."""

    FLAG = "feature.patient_app.enabled"

    async def _stored(self, session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, Any]:
        result = await session.execute(
            select(Hospital.__table__.c.settings).where(Hospital.__table__.c.id == hospital_id)
        )
        return dict(result.scalar_one() or {})

    async def _hospital(self, session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
        result = await session.execute(select(Hospital).where(Hospital.id == hospital_id))
        return result.unique().scalar_one()

    async def test_an_absent_flag_is_switched_on_and_the_gate_opens(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        from app.seeds.seed import ensure_demo_patient_app_flag

        gate = PatientHospitalGate(HospitalRepository(db_session))
        assert await gate.get_enabled(hospital_id) is None

        defaulted = await ensure_demo_patient_app_flag(
            db_session, await self._hospital(db_session, hospital_id)
        )

        assert defaulted is True
        assert (await self._stored(db_session, hospital_id))[self.FLAG] is True
        assert await gate.get_enabled(hospital_id) is not None

    async def test_an_explicit_false_and_other_settings_are_left_alone(
        self, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        from app.seeds.seed import ensure_demo_patient_app_flag

        hospital = await self._hospital(db_session, hospital_id)
        hospital.settings = {self.FLAG: False, "billing.tax_rate": "18.00"}
        await db_session.flush()

        assert await ensure_demo_patient_app_flag(db_session, hospital) is False
        assert await self._stored(db_session, hospital_id) == {
            self.FLAG: False,
            "billing.tax_rate": "18.00",
        }
