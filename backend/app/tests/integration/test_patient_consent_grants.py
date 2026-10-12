"""Attacks on patient consent, record access grants and their tenancy.

``docs/modules/15-patient-app.md`` §6, §7 and §8. Nothing here has an HTTP
caller yet, so the services are driven directly, composed exactly as
``app/api/dependencies/patient.py`` composes them, against a real PostgreSQL.
Links are made by the real :class:`RecordLinkService`, so the consent a test
starts from is the one linking really records.

What is attacked:

* the tables themselves — a row the services would never write must also be
  one the database refuses, and no status is stored anywhere;
* consent — the exact version, what withdrawing ends, and the policy gate
  with a required policy injected;
* creating a grant — who may, for how long, for what;
* :meth:`AccessGrantService.authorize` — one valid read, and every way of
  being one step away from it;
* revocation, and that a grant is never deleted;
* tenancy — the ambient scope hides and refuses another hospital's rows, and
  every method of the new repositories takes its tenant.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy import delete, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.api.dependencies.patient import get_consent_service, get_patient_context
from app.api.dependencies.repositories import (
    get_patient_account_link_repository,
    get_patient_consent_repository,
)
from app.api.dependencies.services import get_audit_sink, get_unit_of_work
from app.core.audit import AuditSink  # noqa: TC001 — FastAPI resolves the override at runtime
from app.core.config import settings
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.tenancy import (
    CrossTenantAccessError,
    TenantScope,
    TenantScopeRequiredError,
    cross_tenant,
    current_tenant_scope,
    is_tenant_scoped,
    tenant_scope,
)
from app.database.unit_of_work import UnitOfWork
from app.models.base import Base
from app.models.hospital import Hospital
from app.models.patient import Gender, Patient
from app.models.patient_account import (
    PatientAccount,
    PatientAccountLink,
    PatientDevice,
    PatientOtpChallenge,
    PatientRefreshToken,
)
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
    PatientDeviceRepository,
    PatientOtpChallengeRepository,
    PatientRefreshTokenRepository,
    PatientRepository,
    UserRepository,
)
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.repositories.base import BaseRepository
from app.schemas.patient_app.account import RegisterRecordRequest
from app.services.audit_service import AuditService
from app.services.auth_throttle import AuthThrottle
from app.services.mrn_service import MRNService
from app.services.patient_app import access_grant_service, consent_service
from app.services.patient_app.access_grant_service import AccessGrantService, Grantee
from app.services.patient_app.common import ClientContext
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.hospital_gate import PatientHospitalGate
from app.services.patient_app.patient_authorization import PatientAuthorization
from app.services.patient_app.policies import CURRENT_POLICIES, DRAFT_VERSION, Policy
from app.services.patient_app.record_link_service import RecordLinkService
from app.services.patient_service import PatientService
from app.tests.billing_helpers import insert_doctor
from app.tests.patient_app_helpers import (
    CSRF,
    PATIENT,
    POLICY,
    FakeSmsSender,
    audit_rows,
    bearer,
    build_patient_application,
    insert_patient_record,
    new_phone,
    open_hospital,
    patient_client,
    sign_in,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.doctor import Doctor

pytestmark = pytest.mark.database

DOB = date(1990, 5, 17)
#: The window of the grant every authorisation test starts from, and a date inside it.
WINDOW = (date(2026, 1, 1), date(2026, 6, 30))
INSIDE = date(2026, 3, 15)
CLIENT = ClientContext(ip_address="203.0.113.9", user_agent="pytest-patient")

#: A platform policy switched to required, as it will be once legal supplies the text.
REQUIRED_TERMS = Policy(ConsentPurpose.TERMS_OF_SERVICE, "2027-01", "platform", required=True)
WITH_REQUIRED_TERMS = (REQUIRED_TERMS, *CURRENT_POLICIES[1:])


# ── Composition ──────────────────────────────────────────────────────────────


@dataclass
class _Services:
    consent: ConsentService
    authorization: PatientAuthorization
    grants: AccessGrantService
    linking: RecordLinkService


def _services(session: AsyncSession, policies: Sequence[Policy] = CURRENT_POLICIES) -> _Services:
    """Compose the patient services the way ``app/api/dependencies/patient.py`` does."""
    uow = UnitOfWork(session)
    audit = AuditService(session, AuditLogRepository(session), UserRepository(session))
    links = PatientAccountLinkRepository(session)
    hospitals = HospitalRepository(session)
    gate = PatientHospitalGate(hospitals)
    consent = ConsentService(
        PatientConsentRepository(session), links, uow, audit, policies=policies
    )
    patient_records = PatientRepository(session)
    patients = PatientService(
        patient_records, MRNService(MrnSequenceRepository(session)), session, audit
    )
    authorization = PatientAuthorization(
        PatientAccountRepository(session), links, gate, patients, consent
    )
    grants = AccessGrantService(
        PatientAccessGrantRepository(session),
        authorization,
        hospitals,
        DoctorRepository(session),
        uow=uow,
        audit=audit,
    )
    linking = RecordLinkService(
        gate,
        links,
        patient_records,
        patients,
        consent,
        throttle=AuthThrottle(AuthThrottleRepository(session), uow),
        uow=uow,
        audit=audit,
    )
    return _Services(consent, authorization, grants, linking)


@dataclass
class _Linked:
    """An account linked to its own record at a hospital, by the real link flow."""

    account: PatientAccount
    patient: Patient
    hospital_id: uuid.UUID

    @property
    def read(self) -> tuple[uuid.UUID, uuid.UUID]:
        """The record and the hospital that holds it, as ``authorize`` takes them."""
        return self.patient.id, self.hospital_id


async def _new_account(session: AsyncSession, phone: str | None = None) -> PatientAccount:
    account = PatientAccount(id=uuid.uuid4(), phone=phone or new_phone(), status="active")
    session.add(account)
    await session.flush()
    await session.commit()
    return account


async def _link(
    session: AsyncSession,
    account: PatientAccount,
    hospital_id: uuid.UUID,
    *,
    version: str = DRAFT_VERSION,
    policies: Sequence[Policy] = CURRENT_POLICIES,
) -> bool:
    """Link through the real service; returns whether a link was created."""
    outcome = await _services(session, policies).linking.link(
        account,
        str(hospital_id),
        date_of_birth=DOB,
        mrn=None,
        consent_policy_version=version,
        client=CLIENT,
    )
    return outcome.created


async def _linked(
    session: AsyncSession, hospital_id: uuid.UUID, *, phone: str | None = None
) -> _Linked:
    account = await _new_account(session, phone)
    patient = await insert_patient_record(session, hospital_id, phone=account.phone)
    assert await _link(session, account, hospital_id)
    return _Linked(account, patient, hospital_id)


async def _extra_hospital(session: AsyncSession, name: str) -> Hospital:
    hospital = Hospital(
        id=uuid.uuid4(),
        name=name,
        slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:10]}",
        address={"line1": "3 Test Road", "city": "Hyderabad", "country": "IN"},
        settings={},
    )
    session.add(hospital)
    await session.flush()
    return await open_hospital(session, hospital.id)


async def _share(
    session: AsyncSession, linked: _Linked, recipient_hospital_id: uuid.UUID, **overrides: Any
) -> uuid.UUID:
    values: dict[str, Any] = {
        "grantee_type": GranteeType.HOSPITAL,
        "grantee_id": recipient_hospital_id,
        "grantee_hospital_id": recipient_hospital_id,
        "categories": [RecordCategory.PRESCRIPTIONS, RecordCategory.LAB_RESULTS],
        "purpose_note": GrantPurposeNote.SECOND_OPINION,
        "client": CLIENT,
    }
    values.update(overrides)
    view = await _services(session).grants.create(linked.account, linked.hospital_id, **values)
    return view.id


async def _authorize(
    session: AsyncSession,
    grantee: Grantee,
    patient_id: uuid.UUID,
    source_hospital_id: uuid.UUID,
    category: RecordCategory = RecordCategory.PRESCRIPTIONS,
    record_date: date | None = INSIDE,
) -> PatientAccessGrant:
    return await _services(session).grants.authorize(
        grantee, patient_id, source_hospital_id, category, record_date
    )


async def _rows(session: AsyncSession, model: Any, *criteria: Any) -> list[Any]:
    result = await session.execute(
        select(model).where(*criteria).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _count(session: AsyncSession, model: Any, *criteria: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*criteria))
    return int(result.scalar_one())


async def _move_record_to(session: AsyncSession, record_id: uuid.UUID, phone: str) -> None:
    await session.execute(update(Patient).where(Patient.id == record_id).values(phone=phone))
    await session.commit()


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    """Hospital A — the source hospital, open to the Patient App."""
    return await open_hospital(db_session, hospital_id)


@pytest_asyncio.fixture
async def other_hospital(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> Hospital:
    """Hospital B — the recipient."""
    return await open_hospital(db_session, other_hospital_id)


@pytest_asyncio.fixture
async def third_hospital(db_session: AsyncSession) -> Hospital:
    """Hospital C — named in no grant."""
    return await _extra_hospital(db_session, "Third Hospital")


@pytest_asyncio.fixture
async def linked(db_session: AsyncSession, hospital: Hospital) -> _Linked:
    return await _linked(db_session, hospital.id)


# ── 1. The tables refuse what the services would never write ─────────────────


async def _refused(session: AsyncSession, statement: Any) -> bool:
    """Whether the database refuses a statement. Leaves the session usable either way."""
    try:
        async with session.begin_nested():
            await session.execute(statement)
    except (IntegrityError, DBAPIError):
        return True
    return False


class TestModelIntegrity:
    def _consent(self, account_id: uuid.UUID, **values: Any) -> Any:
        row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "account_id": account_id,
            "purpose": "terms_of_service",
            "hospital_id": None,
            "policy_version": DRAFT_VERSION,
        }
        return insert(PatientConsentRecord).values(**{**row, **values})

    def _grant(self, linked: _Linked, recipient: uuid.UUID, **values: Any) -> Any:
        now = datetime.now(UTC)
        row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "hospital_id": linked.hospital_id,
            "patient_id": linked.patient.id,
            "grantor_account_id": linked.account.id,
            "grantee_type": "hospital",
            "grantee_id": recipient,
            "grantee_hospital_id": recipient,
            "purpose_note": "consultation",
            "categories": [RecordCategory.IDENTITY],
            "granted_at": now,
            "expires_at": now + timedelta(days=1),
        }
        return insert(PatientAccessGrant).values(**{**row, **values})

    def _link(self, account_id: uuid.UUID, patient: Patient, **values: Any) -> Any:
        row: dict[str, Any] = {
            "id": uuid.uuid4(),
            "account_id": account_id,
            "patient_id": patient.id,
            "hospital_id": patient.hospital_id,
            "relationship": "self",
            "verified_via": "phone_dob",
        }
        return insert(PatientAccountLink).values(**{**row, **values})

    async def test_a_consent_names_a_known_purpose_and_the_right_kind_of_hospital(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        """Attack: file a hospital consent under no hospital, or a platform one under a hospital."""
        account = await _new_account(db_session)

        assert await _refused(db_session, self._consent(account.id, purpose="marketing"))
        for purpose in ("hospital_record_link", "hospital_registration"):
            assert await _refused(db_session, self._consent(account.id, purpose=purpose))
        for purpose in ("terms_of_service", "privacy_notice"):
            assert await _refused(
                db_session, self._consent(account.id, purpose=purpose, hospital_id=hospital.id)
            )
        assert await _refused(db_session, self._consent(uuid.uuid4()))  # no such account
        assert not await _refused(db_session, self._consent(account.id))
        assert not await _refused(
            db_session,
            self._consent(account.id, purpose="hospital_record_link", hospital_id=hospital.id),
        )

    async def test_one_consent_is_in_force_per_purpose_hospital_and_version(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        """Including the platform ones, where the hospital is NULL and NULLs never collide."""
        account, someone_else = await _new_account(db_session), await _new_account(db_session)
        at_a = {"purpose": "hospital_record_link", "hospital_id": hospital.id}
        assert not await _refused(db_session, self._consent(account.id))
        assert not await _refused(db_session, self._consent(account.id, **at_a))

        assert await _refused(db_session, self._consent(account.id))
        assert await _refused(db_session, self._consent(account.id, **at_a))

        # Another version, another hospital, another account: each is its own consent.
        assert not await _refused(db_session, self._consent(account.id, policy_version="2027-01"))
        assert not await _refused(
            db_session, self._consent(account.id, **{**at_a, "hospital_id": other_hospital.id})
        )
        assert not await _refused(db_session, self._consent(someone_else.id))
        # A withdrawn consent is history: the same one may be given again beside it.
        await db_session.execute(
            update(PatientConsentRecord)
            .where(PatientConsentRecord.account_id == account.id)
            .values(withdrawn_at=datetime.now(UTC))
        )
        assert not await _refused(db_session, self._consent(account.id))
        assert not await _refused(db_session, self._consent(account.id, **at_a))

    async def test_a_link_is_a_self_link_made_in_a_known_way(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        account = await _new_account(db_session)
        patient = await insert_patient_record(db_session, hospital.id, phone=account.phone)

        assert await _refused(db_session, self._link(account.id, patient, relationship="parent"))
        assert await _refused(db_session, self._link(account.id, patient, verified_via="staff"))
        assert await _refused(db_session, self._link(account.id, patient, verified_via=""))
        assert await _refused(db_session, self._link(uuid.uuid4(), patient))
        assert not await _refused(db_session, self._link(account.id, patient))

    async def test_the_two_cardinality_rules_are_indexes_and_ended_links_do_not_count(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        """One active link per record; one active self link per account and hospital."""
        intruder = await _new_account(db_session)
        second_record = await insert_patient_record(
            db_session, linked.hospital_id, phone=linked.account.phone
        )

        assert await _refused(db_session, self._link(intruder.id, linked.patient))
        assert await _refused(db_session, self._link(linked.account.id, second_record))

        await db_session.execute(update(PatientAccountLink).values(unlinked_at=datetime.now(UTC)))
        assert not await _refused(db_session, self._link(intruder.id, linked.patient))
        assert not await _refused(db_session, self._link(linked.account.id, second_record))
        assert await _count(db_session, PatientAccountLink) == 3  # the ended one is kept

    @pytest.mark.parametrize(
        "values",
        [
            {"grantee_type": "email"},
            {"grantee_type": "anyone"},
            {"purpose_note": "because I said so"},
            {"categories": []},
            {"expires_at": datetime(2020, 1, 1, tzinfo=UTC)},
            {"records_from": date(2026, 6, 1), "records_to": date(2026, 1, 1)},
            {"revoked_at": datetime.now(UTC)},  # revoked, by nobody
            {"grantor_account_id": uuid.uuid4()},
            {"patient_id": uuid.uuid4()},
            {"grantee_hospital_id": uuid.uuid4()},
            {"hospital_id": uuid.uuid4()},
        ],
    )
    async def test_a_malformed_grant_cannot_be_stored(
        self,
        db_session: AsyncSession,
        linked: _Linked,
        other_hospital: Hospital,
        values: dict[str, Any],
    ) -> None:
        assert await _refused(db_session, self._grant(linked, other_hospital.id, **values))
        assert not await _refused(db_session, self._grant(linked, other_hospital.id))

    async def test_a_grant_revoked_by_somebody_must_say_when(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        assert await _refused(
            db_session,
            self._grant(linked, other_hospital.id, revoked_by_account_id=linked.account.id),
        )

    async def test_record_categories_are_a_database_enum_not_free_text(
        self, db_session: AsyncSession
    ) -> None:
        labels = (
            await db_session.execute(
                text("SELECT unnest(enum_range(NULL::patient_record_category))::text")
            )
        ).scalars()

        assert set(labels) == {category.value for category in RecordCategory}
        assert await _refused(
            db_session, text("SELECT ARRAY['everything']::patient_record_category[]")
        )

    def test_no_status_is_stored_anywhere(self) -> None:
        """A stored status can disagree with the timestamps it is derived from."""
        grant_columns = set(PatientAccessGrant.__table__.c.keys())
        link_columns = set(PatientAccountLink.__table__.c.keys())
        consent_columns = set(PatientConsentRecord.__table__.c.keys())

        for derived in ("status", "state", "is_active", "is_revoked", "is_expired", "suspended"):
            assert derived not in grant_columns | link_columns | consent_columns
        assert {"granted_at", "expires_at", "revoked_at"} <= grant_columns
        assert {"linked_at", "unlinked_at"} <= link_columns
        assert {"granted_at", "withdrawn_at"} <= consent_columns

    def test_a_grants_status_is_derived_from_its_timestamps_alone(self) -> None:
        now = datetime.now(UTC)
        grant = PatientAccessGrant(expires_at=now + timedelta(seconds=1))

        assert grant.status_at(now) is GrantStatus.ACTIVE
        assert grant.status_at(now + timedelta(seconds=1)) is GrantStatus.EXPIRED  # at, not after
        grant.revoked_at = now
        assert grant.status_at(now - timedelta(days=1)) is GrantStatus.REVOKED
        assert grant.status_at(now + timedelta(days=400)) is GrantStatus.REVOKED

    async def test_history_cannot_be_deleted_from_under_its_evidence(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """An account or a record with links, consents or grants cannot simply be removed."""
        await _share(db_session, linked, other_hospital.id)

        assert await _refused(
            db_session, delete(PatientAccount).where(PatientAccount.id == linked.account.id)
        )
        assert await _refused(db_session, delete(Patient).where(Patient.id == linked.patient.id))
        assert await _refused(db_session, delete(Hospital).where(Hospital.id == other_hospital.id))
        assert await _count(db_session, PatientAccessGrant) == 1


# ── 2. Consent, as linking and registering record it ─────────────────────────


class TestConsentOnLinkAndRegister:
    async def test_linking_records_the_exact_version_with_its_evidence(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        [consent] = await _rows(db_session, PatientConsentRecord)

        assert (consent.account_id, consent.purpose, consent.hospital_id) == (
            linked.account.id,
            "hospital_record_link",
            linked.hospital_id,
        )
        assert consent.policy_version == DRAFT_VERSION
        assert consent.withdrawn_at is None
        assert (str(consent.ip_address), consent.user_agent) == ("203.0.113.9", "pytest-patient")
        [event] = await audit_rows(db_session, "patient.consent.granted")
        assert (event.actor_type, event.patient_account_id, event.hospital_id) == (
            "patient",
            linked.account.id,
            linked.hospital_id,
        )
        assert event.context == {"purpose": "hospital_record_link", "policy_version": DRAFT_VERSION}

    @pytest.mark.parametrize("version", ["2025-01", "2026-10-DRAFT", " 2026-10-draft", ""])
    async def test_a_link_under_any_other_version_is_refused_whole(
        self, db_session: AsyncSession, hospital: Hospital, version: str
    ) -> None:
        """Agreeing to a text that has since changed — or to none — is not consent."""
        account = await _new_account(db_session)
        await insert_patient_record(db_session, hospital.id, phone=account.phone)

        with pytest.raises(ConflictError):
            await _link(db_session, account, hospital.id, version=version)

        assert await _count(db_session, PatientConsentRecord) == 0
        assert await _count(db_session, PatientAccountLink) == 0
        assert await audit_rows(db_session, "patient.consent.granted") == []

    async def test_a_link_that_fails_leaves_no_consent_behind(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        """The consent is part of the link: no match, no consent on file."""
        account = await _new_account(db_session)

        with pytest.raises(NotFoundError):
            await _link(db_session, account, hospital.id)

        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_linking_again_does_not_consent_again(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        assert await _link(db_session, linked.account, linked.hospital_id) is False

        assert await _count(db_session, PatientConsentRecord) == 1
        assert len(await audit_rows(db_session, "patient.consent.granted")) == 1

    async def test_a_new_policy_version_stops_access_until_it_is_accepted(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        """Rule 3 of §8.2: fresh acceptance, and the old row kept beside the new."""
        newer = tuple(
            Policy(p.purpose, "2027-01", p.scope, required=p.required)
            if p.purpose is ConsentPurpose.HOSPITAL_RECORD_LINK
            else p
            for p in CURRENT_POLICIES
        )
        services = _services(db_session, newer)

        with pytest.raises(ConsentRequiredError):
            await services.authorization.resolve_context(linked.account, linked.hospital_id)
        with pytest.raises(ConflictError):  # the old version no longer links, either
            await _link(db_session, linked.account, linked.hospital_id, policies=newer)

        await _link(
            db_session, linked.account, linked.hospital_id, version="2027-01", policies=newer
        )

        context = await services.authorization.resolve_context(linked.account, linked.hospital_id)
        assert context.patient_id == linked.patient.id
        consents = await _rows(db_session, PatientConsentRecord)
        assert sorted(c.policy_version for c in consents) == sorted([DRAFT_VERSION, "2027-01"])
        assert await _count(db_session, PatientAccountLink) == 1

    async def test_registering_records_both_consents_at_the_version_shown(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        account = await _new_account(db_session)
        payload = RegisterRecordRequest(
            first_name="Asha",
            last_name="Verma",
            date_of_birth=DOB,
            gender=Gender.FEMALE,
            consent_policy_version=DRAFT_VERSION,
        )

        await _services(db_session).linking.register(
            account, str(hospital.id), payload, client=CLIENT
        )

        consents = await _rows(db_session, PatientConsentRecord)
        assert sorted((c.purpose, c.policy_version, c.hospital_id) for c in consents) == [
            ("hospital_record_link", DRAFT_VERSION, hospital.id),
            ("hospital_registration", DRAFT_VERSION, hospital.id),
        ]
        assert all(str(c.ip_address) == "203.0.113.9" for c in consents)
        granted = await audit_rows(db_session, "patient.consent.granted")
        assert sorted((row.context or {})["purpose"] for row in granted) == [
            "hospital_record_link",
            "hospital_registration",
        ]

    async def test_a_registration_under_another_version_creates_nothing(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        account = await _new_account(db_session)
        payload = RegisterRecordRequest(
            first_name="Asha",
            last_name="Verma",
            date_of_birth=DOB,
            gender=Gender.FEMALE,
            consent_policy_version="2025-01",
        )

        with pytest.raises(ConflictError):
            await _services(db_session).linking.register(
                account, str(hospital.id), payload, client=CLIENT
            )

        assert await _count(db_session, Patient, Patient.phone == account.phone) == 0
        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_a_consent_cannot_be_recorded_against_the_wrong_kind_of_hospital(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        account = await _new_account(db_session)
        consent = _services(db_session).consent

        with pytest.raises(ValidationError):
            await consent.accept(
                account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, policy_version=DRAFT_VERSION
            )
        with pytest.raises(ValidationError):
            await consent.accept(
                account.id,
                ConsentPurpose.PRIVACY_NOTICE,
                policy_version=DRAFT_VERSION,
                hospital_id=hospital.id,
            )

        assert await _count(db_session, PatientConsentRecord) == 0


# ── 3. Withdrawing ───────────────────────────────────────────────────────────


class TestWithdrawal:
    async def test_withdrawing_the_record_link_consent_ends_the_link_and_all_it_gave(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Rule 5 of §8.2 — and §8.3.2: the grants from that link stop working, untouched."""
        await _share(db_session, linked, other_hospital.id)
        services = _services(db_session)
        await _authorize(db_session, Grantee(other_hospital.id), *linked.read)

        ended = await services.consent.withdraw(
            linked.account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            hospital_id=linked.hospital_id,
            client=CLIENT,
        )

        assert ended == 1
        [link] = await _rows(db_session, PatientAccountLink)
        assert link.unlinked_at is not None
        assert link.unlink_reason == "consent_withdrawn"
        [consent] = await _rows(db_session, PatientConsentRecord)
        assert consent.withdrawn_at is not None  # kept as evidence, never deleted
        with pytest.raises(NotFoundError):
            await services.authorization.resolve_context(linked.account, linked.hospital_id)
        assert await services.authorization.describe_links(linked.account) == []
        with pytest.raises(NotFoundError):
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)
        [grant] = await _rows(db_session, PatientAccessGrant)
        assert grant.revoked_at is None
        [withdrawn] = await audit_rows(db_session, "patient.consent.withdrawn")
        [link_ended] = await audit_rows(db_session, "patient.link.ended")
        assert withdrawn.patient_account_id == link_ended.patient_account_id == linked.account.id
        assert withdrawn.hospital_id == link_ended.hospital_id == linked.hospital_id
        assert link_ended.target_id == link.id
        assert link_ended.context == {"reason": "consent_withdrawn"}

    async def test_a_withdrawal_touches_one_account_at_one_hospital(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        """Attack: end somebody else's link, or your own everywhere, with one withdrawal."""
        me_here = await _linked(db_session, hospital.id)
        record_there = await insert_patient_record(
            db_session, other_hospital.id, phone=me_here.account.phone
        )
        assert await _link(db_session, me_here.account, other_hospital.id)
        neighbour = await _linked(db_session, hospital.id)
        services = _services(db_session)

        await services.consent.withdraw(
            me_here.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=hospital.id
        )

        there = await services.authorization.resolve_context(me_here.account, other_hospital.id)
        assert there.patient_id == record_there.id
        theirs = await services.authorization.resolve_context(neighbour.account, hospital.id)
        assert theirs.patient_id == neighbour.patient.id
        assert (
            await _count(
                db_session, PatientConsentRecord, PatientConsentRecord.withdrawn_at.is_(None)
            )
            == 2
        )
        assert (
            await _count(db_session, PatientAccountLink, PatientAccountLink.unlinked_at.is_(None))
            == 2
        )

    async def test_a_withdrawal_that_names_no_hospital_ends_no_hospital_consent(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        """``None`` means "a platform policy" — never "at every hospital"."""
        services = _services(db_session)

        ended = await services.consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK
        )

        assert ended == 0
        await services.authorization.resolve_context(linked.account, linked.hospital_id)
        assert await audit_rows(db_session, "patient.link.ended") == []

    async def test_withdrawing_twice_changes_and_audits_nothing_more(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        services = _services(db_session)

        first = await services.consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )
        [consent] = await _rows(db_session, PatientConsentRecord)
        when = consent.withdrawn_at
        second = await services.consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )

        assert (first, second) == (1, 0)
        [consent] = await _rows(db_session, PatientConsentRecord)
        assert consent.withdrawn_at == when
        assert len(await audit_rows(db_session, "patient.consent.withdrawn")) == 1
        assert len(await audit_rows(db_session, "patient.link.ended")) == 1

    async def test_linking_again_after_a_withdrawal_is_a_new_consent_and_a_new_link(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        await _services(db_session).consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )

        assert await _link(db_session, linked.account, linked.hospital_id) is True

        consents = await _rows(db_session, PatientConsentRecord)
        links = await _rows(db_session, PatientAccountLink)
        assert sorted(c.withdrawn_at is None for c in consents) == [False, True]
        assert sorted(link.unlinked_at is None for link in links) == [False, True]

    @pytest.mark.parametrize("ended_by", ["consent_withdrawn", "superseded"])
    async def test_a_grant_from_an_ended_link_does_not_come_back_with_a_new_link(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital, ended_by: str
    ) -> None:
        await _share(db_session, linked, other_hospital.id)
        if ended_by == "consent_withdrawn":
            await _services(db_session).consent.withdraw(
                linked.account.id,
                ConsentPurpose.HOSPITAL_RECORD_LINK,
                hospital_id=linked.hospital_id,
            )
        else:
            # The hospital moved the record to another number, whose owner linked
            # it, and later moved it back.
            newcomer = await _new_account(db_session)
            await _move_record_to(db_session, linked.patient.id, newcomer.phone)
            assert await _link(db_session, newcomer, linked.hospital_id)
            await _move_record_to(db_session, linked.patient.id, linked.account.phone)
        with pytest.raises(NotFoundError):
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)

        assert await _link(db_session, linked.account, linked.hospital_id) is True

        with pytest.raises(NotFoundError):
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)


# ── 4. The policy gate, with a required policy injected ──────────────────────


class TestPolicyGate:
    async def test_nothing_is_pending_today_and_nothing_optional_ever_is(
        self, db_session: AsyncSession
    ) -> None:
        account = await _new_account(db_session)
        optional = (Policy(ConsentPurpose.PRIVACY_NOTICE, "2027-01", "platform", required=False),)

        assert await _services(db_session).consent.pending_policies(account.id) == []
        assert await _services(db_session, optional).consent.pending_policies(account.id) == []

    async def test_a_required_policy_blocks_every_patient_action_until_it_is_accepted(
        self,
        db_session: AsyncSession,
        linked: _Linked,
        hospital: Hospital,
        other_hospital: Hospital,
    ) -> None:
        """The same account, the same link, the same request — only the policy changed."""
        gated = _services(db_session, WITH_REQUIRED_TERMS)
        newcomer = await _new_account(db_session)
        await insert_patient_record(db_session, hospital.id, phone=newcomer.phone)
        payload = RegisterRecordRequest(
            first_name="Asha",
            last_name="Verma",
            date_of_birth=date(1971, 1, 1),
            gender=Gender.FEMALE,
            consent_policy_version=DRAFT_VERSION,
        )
        share: dict[str, Any] = {
            "grantee_type": GranteeType.HOSPITAL,
            "grantee_id": other_hospital.id,
            "grantee_hospital_id": other_hospital.id,
            "categories": [RecordCategory.IDENTITY],
            "purpose_note": GrantPurposeNote.CONSULTATION,
        }
        blocked: list[Callable[[], Awaitable[object]]] = [
            lambda: gated.consent.ensure_policies_accepted(linked.account.id),
            lambda: gated.authorization.resolve_context(linked.account, hospital.id),
            lambda: gated.grants.create(linked.account, hospital.id, **share),
            lambda: _link(db_session, newcomer, hospital.id, policies=WITH_REQUIRED_TERMS),
            lambda: gated.linking.register(newcomer, str(hospital.id), payload, client=CLIENT),
        ]

        for action in blocked:
            with pytest.raises(ConsentRequiredError):
                await action()
        assert await _count(db_session, PatientAccountLink) == 1
        assert await _count(db_session, PatientAccessGrant) == 0
        assert await _count(db_session, Patient, Patient.phone == newcomer.phone) == 1

        for account in (linked.account, newcomer):
            await gated.consent.accept(
                account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01", client=CLIENT
            )
        for action in blocked[:4]:
            await action()
        assert await _count(db_session, PatientAccessGrant) == 1
        assert await _count(db_session, PatientAccountLink) == 2

    async def test_only_the_accounts_own_acceptance_of_the_current_version_counts(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        """Attack: pass the gate on an old acceptance, on another purpose, or on somebody else's."""
        account, someone_else = await _new_account(db_session), await _new_account(db_session)
        older = _services(
            db_session,
            (Policy(ConsentPurpose.TERMS_OF_SERVICE, "2026-01", "platform", required=True),),
        )
        gated = _services(db_session, (REQUIRED_TERMS, *CURRENT_POLICIES[1:]))
        await older.consent.accept(
            account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2026-01"
        )
        await gated.consent.accept(
            account.id, ConsentPurpose.PRIVACY_NOTICE, policy_version=DRAFT_VERSION
        )
        await gated.consent.accept(
            someone_else.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01"
        )

        with pytest.raises(ConflictError):  # the old text can no longer be accepted
            await gated.consent.accept(
                account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2026-01"
            )
        pending = await gated.consent.pending_policies(account.id)

        assert [(p.purpose, p.version) for p in pending] == [("terms_of_service", "2027-01")]
        with pytest.raises(ConsentRequiredError):
            await gated.consent.ensure_policies_accepted(account.id)
        await gated.consent.ensure_policies_accepted(someone_else.id)

    async def test_withdrawing_a_required_policy_closes_the_gate_again(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        gated = _services(db_session, WITH_REQUIRED_TERMS)
        await gated.consent.accept(
            linked.account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01"
        )
        await gated.authorization.resolve_context(linked.account, linked.hospital_id)

        assert await gated.consent.withdraw(linked.account.id, ConsentPurpose.TERMS_OF_SERVICE) == 1

        with pytest.raises(ConsentRequiredError):
            await gated.authorization.resolve_context(linked.account, linked.hospital_id)
        # A platform withdrawal ends no hospital link: the link is there when they accept again.
        assert (
            await _count(db_session, PatientAccountLink, PatientAccountLink.unlinked_at.is_(None))
            == 1
        )
        await gated.consent.accept(
            linked.account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01"
        )
        await gated.authorization.resolve_context(linked.account, linked.hospital_id)
        terms = await _rows(
            db_session, PatientConsentRecord, PatientConsentRecord.purpose == "terms_of_service"
        )
        assert sorted(row.withdrawn_at is None for row in terms) == [False, True]
        assert {row.hospital_id for row in terms} == {None}

    async def test_the_gate_is_asked_before_anything_about_the_hospital_is_revealed(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        """A pending policy is the answer for a hospital that does not exist, too."""
        gated = _services(db_session, WITH_REQUIRED_TERMS)

        with pytest.raises(ConsentRequiredError):
            await gated.authorization.resolve_context(linked.account, uuid.uuid4())


# ── 5. Creating a grant ──────────────────────────────────────────────────────


async def _end_link(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(update(PatientAccountLink).values(unlinked_at=datetime.now(UTC)))


async def _move_phone(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(
        update(Patient).where(Patient.id == linked.patient.id).values(phone=new_phone())
    )


async def _deactivate_record(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(
        update(Patient).where(Patient.id == linked.patient.id).values(deleted_at=datetime.now(UTC))
    )


async def _suspend_account(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(
        update(PatientAccount)
        .where(PatientAccount.id == linked.account.id)
        .values(status="suspended")
    )


async def _close_account(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(
        update(PatientAccount).where(PatientAccount.id == linked.account.id).values(status="closed")
    )


async def _switch_patient_app_off(session: AsyncSession, linked: _Linked) -> None:
    await open_hospital(session, linked.hospital_id, enabled=False)


async def _deactivate_hospital(session: AsyncSession, linked: _Linked) -> None:
    await session.execute(
        update(Hospital).where(Hospital.id == linked.hospital_id).values(is_active=False)
    )


#: Every way the grantor's standing at the source hospital can stop being live.
_GRANTOR_NO_LONGER_STANDS: dict[str, Callable[[AsyncSession, _Linked], Awaitable[None]]] = {
    "link ended": _end_link,
    "link suspended (phone changed)": _move_phone,
    "record deactivated": _deactivate_record,
    "account suspended": _suspend_account,
    "account closed": _close_account,
    "patient app switched off": _switch_patient_app_off,
    "hospital deactivated": _deactivate_hospital,
}


class TestCreateGrant:
    @pytest.mark.parametrize(
        "why",
        [name for name in _GRANTOR_NO_LONGER_STANDS if not name.startswith("account")],
    )
    async def test_it_needs_an_active_unsuspended_self_link(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital, why: str
    ) -> None:
        await _GRANTOR_NO_LONGER_STANDS[why](db_session, linked)
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await _share(db_session, linked, other_hospital.id)

        assert await _count(db_session, PatientAccessGrant) == 0
        assert await audit_rows(db_session, "patient.access_grant.created") == []

    async def test_a_link_somewhere_else_or_somebody_elses_link_is_not_enough(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Attack: share a record you do not hold — at another hospital, or another person's."""
        never_linked = await _new_account(db_session)
        await insert_patient_record(db_session, linked.hospital_id, phone=never_linked.phone)
        linked_elsewhere = await _linked(db_session, other_hospital.id)

        for grantor in (never_linked, linked_elsewhere.account):
            with pytest.raises(NotFoundError):
                await _share(
                    db_session,
                    _Linked(grantor, linked.patient, linked.hospital_id),
                    other_hospital.id,
                )

        assert await _count(db_session, PatientAccessGrant) == 0

    async def test_it_needs_the_record_link_consent_in_force(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await db_session.execute(
            update(PatientConsentRecord).values(withdrawn_at=datetime.now(UTC))
        )
        await db_session.commit()

        with pytest.raises(ConsentRequiredError):
            await _share(db_session, linked, other_hospital.id)

        assert await _count(db_session, PatientAccessGrant) == 0

    def test_the_record_shared_is_never_an_argument(self) -> None:
        """The patient record comes from the grantor's own link. There is nowhere to name one."""
        parameters = set(inspect.signature(AccessGrantService.create).parameters)

        assert "patient_id" not in parameters
        assert not {name for name in parameters if "patient" in name}

    async def test_the_grant_is_on_the_grantors_own_record_at_the_source_hospital(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        mine = await _linked(db_session, hospital.id)
        await _linked(db_session, hospital.id)  # a neighbour, whose record must not be touched

        grant_id = await _share(db_session, mine, other_hospital.id)

        [grant] = await _rows(db_session, PatientAccessGrant)
        assert (grant.id, grant.hospital_id, grant.patient_id, grant.grantor_account_id) == (
            grant_id,
            hospital.id,
            mine.patient.id,
            mine.account.id,
        )
        [event] = await audit_rows(db_session, "patient.access_grant.created")
        assert (event.actor_type, event.patient_account_id, event.hospital_id, event.target_id) == (
            "patient",
            mine.account.id,
            hospital.id,
            grant_id,
        )
        assert str(event.ip_address) == "203.0.113.9"
        assert mine.account.phone not in str(event.context)

    async def test_it_lasts_thirty_days_unless_told_and_never_more_than_a_year(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        now = datetime.now(UTC)
        minute = timedelta(minutes=1)

        await _share(db_session, linked, other_hospital.id)
        await _share(
            db_session, linked, other_hospital.id, expires_at=now + timedelta(days=365) - minute
        )
        await _share(db_session, linked, other_hospital.id, expires_at=now + timedelta(hours=1))
        for refused in (
            now + timedelta(days=365) + minute,
            now + timedelta(days=3650),
            now - minute,
            now,
            (now + timedelta(days=5)).replace(tzinfo=None),  # no time zone: which instant?
        ):
            with pytest.raises(ValidationError):
                await _share(db_session, linked, other_hospital.id, expires_at=refused)

        grants = await _rows(db_session, PatientAccessGrant)
        lifetimes = sorted(grant.expires_at - grant.granted_at for grant in grants)
        assert len(lifetimes) == 3
        assert lifetimes[1] == timedelta(days=30)
        assert lifetimes[2] <= timedelta(days=365)

    async def test_a_time_zone_cannot_stretch_the_year(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Attack: an expiry that reads as within a year in its own zone and is not in UTC."""
        from datetime import timezone

        far_west = timezone(timedelta(hours=-12))
        expiry = (datetime.now(UTC) + timedelta(days=365, hours=6)).astimezone(far_west)

        with pytest.raises(ValidationError):
            await _share(db_session, linked, other_hospital.id, expires_at=expiry)

    @pytest.mark.parametrize(
        "categories",
        [
            [],
            [RecordCategory.DOCUMENTS],
            [RecordCategory.PRESCRIPTIONS, RecordCategory.DOCUMENTS],
            ["prescriptions"],  # free text, even when it spells a category
            ["everything"],
            [RecordCategory.IDENTITY, "*"],
        ],
    )
    async def test_categories_are_chosen_from_the_list_and_documents_cannot_be(
        self,
        db_session: AsyncSession,
        linked: _Linked,
        other_hospital: Hospital,
        categories: list[Any],
    ) -> None:
        with pytest.raises(ValidationError):
            await _share(db_session, linked, other_hospital.id, categories=categories)

        assert await _count(db_session, PatientAccessGrant) == 0

    async def test_the_context_and_the_window_are_validated(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        for overrides in (
            {"context_appointment_id": uuid.uuid4()},  # nothing can verify it yet
            {"records_from": date(2026, 6, 1), "records_to": date(2026, 1, 1)},
        ):
            with pytest.raises(ValidationError):
                await _share(db_session, linked, other_hospital.id, **overrides)

        assert await _count(db_session, PatientAccessGrant) == 0

    async def test_the_recipient_is_a_real_hospital_or_one_of_its_doctors(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """A grantee is never an arbitrary id, an inactive hospital or a doctor from elsewhere."""
        closed = await _extra_hospital(db_session, "Closed Hospital")
        await db_session.execute(
            update(Hospital).where(Hospital.id == closed.id).values(is_active=False)
        )
        doctor_at_source = await insert_doctor(db_session, linked.hospital_id)
        await db_session.commit()
        b = other_hospital.id

        for overrides in (
            {"grantee_hospital_id": uuid.uuid4()},
            {"grantee_id": closed.id, "grantee_hospital_id": closed.id},
            {"grantee_id": uuid.uuid4()},  # a hospital grant that names something else
            {"grantee_type": GranteeType.DOCTOR, "grantee_id": b},  # a hospital is not a doctor
            {"grantee_type": GranteeType.DOCTOR, "grantee_id": doctor_at_source.id},
            {"grantee_type": GranteeType.DOCTOR, "grantee_id": uuid.uuid4()},
        ):
            with pytest.raises(ValidationError):
                await _share(db_session, linked, b, **overrides)

        assert await _count(db_session, PatientAccessGrant) == 0


# ── 6. authorize(): one valid read, and everything one step away from it ─────


@dataclass
class _Shared:
    """Hospital A's patient has shared prescriptions and lab results, Jan–Jun 2026, with B."""

    session: AsyncSession
    linked: _Linked
    recipient: Hospital
    elsewhere: Hospital
    grant_id: uuid.UUID

    @property
    def reader(self) -> Grantee:
        return Grantee(self.recipient.id)

    async def read(self, **changes: Any) -> PatientAccessGrant:
        """The one read the grant is for, with any one thing changed."""
        call: dict[str, Any] = {
            "grantee": self.reader,
            "patient_id": self.linked.patient.id,
            "source_hospital_id": self.linked.hospital_id,
            "category": RecordCategory.PRESCRIPTIONS,
            "record_date": INSIDE,
        }
        call.update(changes)
        return await _authorize(self.session, **call)


@pytest_asyncio.fixture
async def shared(
    db_session: AsyncSession, linked: _Linked, other_hospital: Hospital, third_hospital: Hospital
) -> _Shared:
    grant_id = await _share(
        db_session, linked, other_hospital.id, records_from=WINDOW[0], records_to=WINDOW[1]
    )
    return _Shared(db_session, linked, other_hospital, third_hospital, grant_id)


class TestAuthorize:
    async def test_the_exact_intended_read_is_allowed_and_names_its_grant(
        self, shared: _Shared
    ) -> None:
        for category in (RecordCategory.PRESCRIPTIONS, RecordCategory.LAB_RESULTS):
            for day in (WINDOW[0], INSIDE, WINDOW[1]):
                grant = await shared.read(category=category, record_date=day)
                assert grant.id == shared.grant_id

    async def test_checking_writes_nothing(self, shared: _Shared, db_session: AsyncSession) -> None:
        """The gate decides; it does not record, cache or change anything."""
        from app.models.audit_log import AuditLog

        before = await _count(db_session, AuditLog)

        await shared.read()
        with pytest.raises(NotFoundError):
            await shared.read(category=RecordCategory.IDENTITY)

        assert await _count(db_session, AuditLog) == before
        assert not db_session.new
        assert not db_session.dirty
        assert current_tenant_scope() is None

    async def test_with_no_grant_at_all_the_answer_is_not_found(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        with pytest.raises(NotFoundError) as refused:
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)

        assert refused.value.status_code == 404  # never 403: it does not confirm the record

    async def test_a_grant_from_a_to_b_never_authorises_c(self, shared: _Shared) -> None:
        """Attack: staff of an unrelated hospital, a doctor of one, or the grant's id reused."""
        c = shared.elsewhere.id
        doctor_at_c = await insert_doctor(shared.session, c)
        await shared.session.commit()

        for grantee in (Grantee(c), Grantee(c, doctor_at_c.id), Grantee(c, shared.recipient.id)):
            with pytest.raises(NotFoundError):
                await shared.read(grantee=grantee)

    async def test_staff_of_the_source_hospital_gain_nothing_from_a_grant_to_b(
        self, shared: _Shared
    ) -> None:
        """Their own access is staff RBAC. A grant to somebody else is not theirs to use."""
        with pytest.raises(NotFoundError):
            await shared.read(grantee=Grantee(shared.linked.hospital_id))

    @pytest.mark.parametrize(
        "category",
        [
            RecordCategory.IDENTITY,
            RecordCategory.MEDICAL_HISTORY,
            RecordCategory.APPOINTMENTS,
            RecordCategory.DOCUMENTS,
        ],
    )
    async def test_a_category_that_was_not_granted_is_refused(
        self, shared: _Shared, category: RecordCategory
    ) -> None:
        """No category implies another."""
        with pytest.raises(NotFoundError):
            await shared.read(category=category)

    async def test_documents_are_never_readable_even_if_a_row_says_so(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """Attack: a grant row that carries ``documents`` anyway (a bug, or a hand-made row)."""
        await db_session.execute(
            update(PatientAccessGrant).values(
                categories=[RecordCategory.DOCUMENTS, RecordCategory.PRESCRIPTIONS]
            )
        )
        await db_session.commit()

        await shared.read()
        with pytest.raises(NotFoundError):
            await shared.read(category=RecordCategory.DOCUMENTS)

    @pytest.mark.parametrize(
        "record_date",
        [date(2025, 12, 31), date(2026, 7, 1), date(1990, 1, 1), date(2099, 1, 1), None],
    )
    async def test_a_record_outside_the_window_is_refused(
        self, shared: _Shared, record_date: date | None
    ) -> None:
        """A record with no date is not inside any window."""
        with pytest.raises(NotFoundError):
            await shared.read(record_date=record_date)

    async def test_a_half_open_window_is_still_a_window(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        reader, edge = Grantee(other_hospital.id), date(2026, 4, 1)
        since = await _share(db_session, linked, other_hospital.id, records_from=edge)
        until = await _share(
            db_session,
            linked,
            other_hospital.id,
            records_to=edge - timedelta(days=30),
            categories=[RecordCategory.IDENTITY],
        )

        on_the_edge = await _authorize(db_session, reader, *linked.read, record_date=edge)
        before_it = await _authorize(
            db_session,
            reader,
            *linked.read,
            category=RecordCategory.IDENTITY,
            record_date=date(2026, 1, 1),
        )

        assert (on_the_edge.id, before_it.id) == (since, until)
        for category, day in (
            (RecordCategory.PRESCRIPTIONS, edge - timedelta(days=1)),
            (RecordCategory.PRESCRIPTIONS, None),
            (RecordCategory.IDENTITY, edge),
            (RecordCategory.IDENTITY, None),
        ):
            with pytest.raises(NotFoundError):
                await _authorize(
                    db_session, reader, *linked.read, category=category, record_date=day
                )

    async def test_a_revoked_grant_is_refused_on_the_very_next_read(self, shared: _Shared) -> None:
        await shared.read()

        await _services(shared.session).grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id
        )

        with pytest.raises(NotFoundError):
            await shared.read()

    async def test_an_expired_grant_is_refused_from_the_instant_it_expires(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        now = datetime.now(UTC)
        # A grant is only ever given under a link that already exists, so the
        # link is moved back with it: a grant older than its grantor's link is
        # a grant from an ended link, and is refused for that reason instead.
        await db_session.execute(
            update(PatientAccountLink).values(linked_at=now - timedelta(days=31))
        )
        await db_session.execute(
            update(PatientAccessGrant).values(
                granted_at=now - timedelta(days=30), expires_at=now + timedelta(seconds=30)
            )
        )
        await db_session.commit()
        await shared.read()

        await db_session.execute(update(PatientAccessGrant).values(expires_at=now))
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await shared.read()
        [listed] = await _services(db_session).grants.list_for_account(
            shared.linked.account, shared.linked.hospital_id
        )
        assert listed.status is GrantStatus.EXPIRED
        [row] = await _rows(db_session, PatientAccessGrant)
        assert row.revoked_at is None  # expiry is derived; nothing was written

    @pytest.mark.parametrize("why", list(_GRANTOR_NO_LONGER_STANDS))
    async def test_a_grant_stops_working_when_its_grantor_no_longer_stands_behind_it(
        self, shared: _Shared, db_session: AsyncSession, why: str
    ) -> None:
        """§8.3.2: unusable at once, and the grant row is not touched."""
        await shared.read()

        await _GRANTOR_NO_LONGER_STANDS[why](db_session, shared.linked)
        await db_session.commit()

        with pytest.raises(NotFoundError):
            await shared.read()
        [row] = await _rows(db_session, PatientAccessGrant)
        assert (row.revoked_at, row.revoked_by_account_id) == (None, None)

    async def test_a_suspension_that_ends_gives_the_grant_back_and_a_revocation_never_does(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        phone = shared.linked.account.phone
        await _move_record_to(db_session, shared.linked.patient.id, new_phone())
        with pytest.raises(NotFoundError):
            await shared.read()

        await _move_record_to(db_session, shared.linked.patient.id, phone)
        await shared.read()

        await _services(db_session).grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id
        )
        with pytest.raises(NotFoundError):
            await shared.read()

    async def test_a_grant_on_one_record_reaches_no_other_record_and_no_other_hospital(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """Attack: the same person's record at C, a neighbour's record at A, the hospitals swapped."""
        neighbour = await _linked(db_session, shared.linked.hospital_id)
        same_person_at_c = await insert_patient_record(
            db_session, shared.elsewhere.id, phone=shared.linked.account.phone
        )
        assert await _link(db_session, shared.linked.account, shared.elsewhere.id)
        mine = shared.linked.patient.id

        for patient_id, source in (
            (neighbour.patient.id, shared.linked.hospital_id),
            (same_person_at_c.id, shared.elsewhere.id),
            (same_person_at_c.id, shared.linked.hospital_id),
            (mine, shared.elsewhere.id),
            (mine, shared.recipient.id),
            (uuid.uuid4(), shared.linked.hospital_id),
        ):
            with pytest.raises(NotFoundError):
                await shared.read(patient_id=patient_id, source_hospital_id=source)

    async def test_the_grant_given_by_a_numbers_previous_holder_does_not_pass_to_the_next(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """The record moved to a new number and its new owner linked it. They shared nothing."""
        newcomer = await _new_account(db_session)
        await _move_record_to(db_session, shared.linked.patient.id, newcomer.phone)
        assert await _link(db_session, newcomer, shared.linked.hospital_id)
        services = _services(db_session)

        with pytest.raises(NotFoundError):
            await shared.read()
        assert await services.grants.list_for_account(newcomer, shared.linked.hospital_id) == []
        with pytest.raises(NotFoundError):
            await services.grants.revoke(newcomer, shared.linked.hospital_id, shared.grant_id)

    async def test_one_grant_does_not_stand_in_for_another(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """A newer grant that does not cover the read does not hide the one that does."""
        narrower = await _share(
            db_session, shared.linked, shared.recipient.id, categories=[RecordCategory.IDENTITY]
        )
        services = _services(db_session)

        assert (await shared.read()).id == shared.grant_id
        assert (
            await shared.read(category=RecordCategory.IDENTITY, record_date=None)
        ).id == narrower

        await services.grants.revoke(shared.linked.account, shared.linked.hospital_id, narrower)
        assert (await shared.read()).id == shared.grant_id
        with pytest.raises(NotFoundError):
            await shared.read(category=RecordCategory.IDENTITY, record_date=None)

        await services.grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id
        )
        with pytest.raises(NotFoundError):
            await shared.read()

    async def test_a_doctor_grant_is_for_that_doctor_alone(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        doctor: Doctor = await insert_doctor(db_session, other_hospital.id)
        colleague: Doctor = await insert_doctor(db_session, other_hospital.id)
        await db_session.commit()
        b = other_hospital.id
        grant_id = await _share(
            db_session, linked, b, grantee_type=GranteeType.DOCTOR, grantee_id=doctor.id
        )

        allowed = await _authorize(db_session, Grantee(b, doctor.id), *linked.read)

        assert allowed.id == grant_id
        for grantee in (
            Grantee(b, colleague.id),
            Grantee(b),  # staff of B who are not that doctor
            Grantee(b, b),
            Grantee(linked.hospital_id, doctor.id),  # the right doctor, at the wrong hospital
        ):
            with pytest.raises(NotFoundError):
                await _authorize(db_session, grantee, *linked.read)

    async def test_a_hospital_grant_is_for_any_staff_of_that_hospital(
        self, shared: _Shared
    ) -> None:
        doctor = await insert_doctor(shared.session, shared.recipient.id)
        await shared.session.commit()

        grant = await shared.read(grantee=Grantee(shared.recipient.id, doctor.id))

        assert grant.id == shared.grant_id

    async def test_under_the_readers_own_tenant_scope_the_gate_fails_closed(
        self, shared: _Shared
    ) -> None:
        """A staff request is confined to its own hospital; the grant lives in the source's rows.

        Called as a staff request would call it, the gate sees no grant and
        refuses. The reading side has to ask under an explicit system scope.
        """
        with tenant_scope(TenantScope.hospital(shared.recipient.id)), pytest.raises(NotFoundError):
            await shared.read()

        with tenant_scope(TenantScope.system("reading a record shared by a patient's grant")):
            assert (await shared.read()).id == shared.grant_id


# ── 7. Revoking, and never deleting ──────────────────────────────────────────


class TestRevoke:
    async def test_revoking_is_audited_once_however_often_it_is_asked(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        services = _services(db_session)
        revoke = (shared.linked.account, shared.linked.hospital_id, shared.grant_id)

        first = await services.grants.revoke(*revoke, reason="No longer needed", client=CLIENT)
        second = await services.grants.revoke(*revoke, reason="Changed my mind about why")
        third = await services.grants.revoke(*revoke)

        assert first.status is second.status is third.status is GrantStatus.REVOKED
        assert first.revoked_at == second.revoked_at == third.revoked_at
        [row] = await _rows(db_session, PatientAccessGrant)
        assert (row.revoked_by_account_id, row.revoke_reason) == (
            shared.linked.account.id,
            "No longer needed",
        )
        [event] = await audit_rows(db_session, "patient.access_grant.revoked")
        assert (event.actor_type, event.patient_account_id, event.hospital_id, event.target_id) == (
            "patient",
            shared.linked.account.id,
            shared.linked.hospital_id,
            shared.grant_id,
        )
        assert "No longer needed" not in str(event.context)  # free text stays out of the audit

    async def test_only_the_grantor_can_revoke_and_only_at_the_source_hospital(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """Attack: revoke — or probe for — a grant by its id."""
        neighbour = await _linked(db_session, shared.linked.hospital_id)
        services = _services(db_session)

        for account, hospital_id, grant_id in (
            (neighbour.account, shared.linked.hospital_id, shared.grant_id),
            (shared.linked.account, shared.recipient.id, shared.grant_id),
            (shared.linked.account, shared.elsewhere.id, shared.grant_id),
            (shared.linked.account, shared.linked.hospital_id, uuid.uuid4()),
        ):
            with pytest.raises(NotFoundError):
                await services.grants.revoke(account, hospital_id, grant_id)

        await shared.read()
        assert await audit_rows(db_session, "patient.access_grant.revoked") == []

    @pytest.mark.parametrize("why", ["link ended", "link suspended (phone changed)"])
    async def test_a_patient_can_always_take_back_what_they_gave(
        self, shared: _Shared, db_session: AsyncSession, why: str
    ) -> None:
        await _GRANTOR_NO_LONGER_STANDS[why](db_session, shared.linked)
        await db_session.commit()

        view = await _services(db_session).grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id
        )

        assert view.status is GrantStatus.REVOKED

    async def test_a_long_reason_is_cut_not_refused(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        await _services(db_session).grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id, reason="x" * 5000
        )

        [row] = await _rows(db_session, PatientAccessGrant)
        assert row.revoke_reason == "x" * 200

    async def test_a_grant_is_never_deleted(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """Revoked, expired, its link ended: the row and its audit history stay."""
        services = _services(db_session)
        expired = await _share(db_session, shared.linked, shared.recipient.id)
        await db_session.execute(
            update(PatientAccessGrant)
            .where(PatientAccessGrant.id == expired)
            .values(
                granted_at=datetime.now(UTC) - timedelta(days=60),
                expires_at=datetime.now(UTC) - timedelta(days=30),
            )
        )
        await services.grants.revoke(
            shared.linked.account, shared.linked.hospital_id, shared.grant_id
        )
        await services.consent.withdraw(
            shared.linked.account.id,
            ConsentPurpose.HOSPITAL_RECORD_LINK,
            hospital_id=shared.linked.hospital_id,
        )

        listed = await services.grants.list_for_account(
            shared.linked.account, shared.linked.hospital_id
        )

        assert {grant.id: grant.status for grant in listed} == {
            shared.grant_id: GrantStatus.REVOKED,
            expired: GrantStatus.EXPIRED,
        }
        assert await _count(db_session, PatientAccessGrant) == 2
        assert len(await audit_rows(db_session, "patient.access_grant.created")) == 2

    def test_no_patient_service_has_a_way_to_delete_a_grant_or_a_consent(self) -> None:
        for module in (access_grant_service, consent_service):
            source = inspect.getsource(module)
            for forbidden in ("hard_delete", "soft_delete", ".delete(", "delete("):
                assert forbidden not in source, (module.__name__, forbidden)
        for repository in (PatientAccessGrantRepository, PatientConsentRepository):
            assert not [name for name in vars(repository) if "delete" in name or "purge" in name]

    async def test_an_account_lists_only_what_it_gave_at_that_hospital(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        neighbour = await _linked(db_session, shared.linked.hospital_id)
        await _share(db_session, neighbour, shared.recipient.id)
        services = _services(db_session)

        mine = await services.grants.list_for_account(
            shared.linked.account, shared.linked.hospital_id
        )

        assert [grant.id for grant in mine] == [shared.grant_id]
        for hospital_id in (shared.recipient.id, shared.elsewhere.id):
            assert await services.grants.list_for_account(shared.linked.account, hospital_id) == []


# ── 8. Tenancy ───────────────────────────────────────────────────────────────

_TENANT_REPOSITORIES = (
    PatientAccountLinkRepository,
    PatientConsentRepository,
    PatientAccessGrantRepository,
)
_PLATFORM_REPOSITORIES = (
    (PatientAccountRepository, PatientAccount),
    (PatientOtpChallengeRepository, PatientOtpChallenge),
    (PatientRefreshTokenRepository, PatientRefreshToken),
    (PatientDeviceRepository, PatientDevice),
)


class TestTenancy:
    async def test_the_ambient_scope_hides_another_hospitals_rows(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """Layer 2: under hospital B's scope, hospital A's links, consents and grants are not there.

        Asked with plain statements that name no hospital at all — the mistake
        the guard exists for.
        """
        models = (PatientAccountLink, PatientConsentRecord, PatientAccessGrant)

        for model in models:
            with tenant_scope(TenantScope.hospital(shared.linked.hospital_id)):
                assert len(await _rows(db_session, model)) == 1
            for elsewhere in (shared.recipient.id, shared.elsewhere.id):
                with tenant_scope(TenantScope.hospital(elsewhere)):
                    assert await _rows(db_session, model) == []
                    assert await _count(db_session, model) == 0

    async def test_being_named_in_a_grant_does_not_put_it_in_the_recipients_scope(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """A grant belongs to the source hospital; the recipient's ordinary queries never see it."""
        grants = PatientAccessGrantRepository(db_session)

        with tenant_scope(TenantScope.hospital(shared.recipient.id)):
            assert (
                await grants.list_unrevoked_for_recipient(
                    shared.linked.hospital_id,
                    patient_id=shared.linked.patient.id,
                    grantee_hospital_id=shared.recipient.id,
                )
                == []
            )
            assert await grants.get_by_id(shared.grant_id, shared.recipient.id) is None
            assert await grants.get_by_id(shared.grant_id, cross_tenant("probe")) is None
            with pytest.raises(TenantScopeRequiredError):
                await grants.get_by_id(shared.grant_id)
            assert (
                await db_session.execute(
                    update(PatientAccessGrant)
                    .where(PatientAccessGrant.id == shared.grant_id)
                    .values(expires_at=datetime.now(UTC) + timedelta(days=300))
                )
            ).rowcount == 0  # type: ignore[attr-defined]

    async def test_a_platform_consent_belongs_to_no_hospital_and_a_hospital_one_to_one(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        consents = PatientConsentRepository(db_session)
        await _services(db_session).consent.accept(
            linked.account.id, ConsentPurpose.PRIVACY_NOTICE, policy_version=DRAFT_VERSION
        )
        hospital_one: dict[str, Any] = {
            "account_id": linked.account.id,
            "purpose": "hospital_record_link",
            "policy_version": DRAFT_VERSION,
        }
        platform_one = {**hospital_one, "purpose": "privacy_notice"}

        assert await consents.get_active(linked.hospital_id, **hospital_one) is not None
        assert await consents.get_active(other_hospital.id, **hospital_one) is None
        assert await consents.get_active(None, **hospital_one) is None  # None is not "anywhere"
        assert await consents.get_active(None, **platform_one) is not None
        assert await consents.get_active(linked.hospital_id, **platform_one) is None
        assert not await consents.has_ever(
            other_hospital.id, **{k: v for k, v in hospital_one.items() if k != "policy_version"}
        )
        with tenant_scope(TenantScope.hospital(other_hospital.id)):
            assert await consents.get_active(linked.hospital_id, **hospital_one) is None
            assert (
                await consents.list_active(
                    linked.hospital_id, account_id=linked.account.id, purpose="hospital_record_link"
                )
                == []
            )

    async def test_a_scope_refuses_to_write_another_hospitals_row(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Attack: under hospital B's scope, create a link, a consent or a grant in hospital A."""
        account = await _new_account(db_session)
        patient = await insert_patient_record(db_session, linked.hospital_id, phone=account.phone)
        now = datetime.now(UTC)
        a, b = linked.hospital_id, other_hospital.id

        async def _a_link() -> object:
            return await PatientAccountLinkRepository(db_session).create_link(
                a, account_id=account.id, patient_id=patient.id, verified_via="phone_dob", now=now
            )

        async def _a_consent() -> object:
            return await PatientConsentRepository(db_session).add(
                a,
                account_id=account.id,
                purpose="hospital_record_link",
                policy_version=DRAFT_VERSION,
                now=now,
                ip_address=None,
                user_agent=None,
            )

        async def _a_grant() -> object:
            return await PatientAccessGrantRepository(db_session).create_grant(
                a,
                patient_id=linked.patient.id,
                grantor_account_id=linked.account.id,
                grantee_type="hospital",
                grantee_id=b,
                grantee_hospital_id=b,
                purpose_note="consultation",
                categories=[RecordCategory.IDENTITY],
                records_from=None,
                records_to=None,
                granted_at=now,
                expires_at=now + timedelta(days=1),
            )

        for write in (_a_link, _a_consent, _a_grant):
            with pytest.raises(CrossTenantAccessError):
                async with db_session.begin_nested():
                    with tenant_scope(TenantScope.hospital(b)):
                        await write()

        assert await _count(db_session, PatientAccountLink) == 1
        assert await _count(db_session, PatientConsentRecord) == 1
        assert await _count(db_session, PatientAccessGrant) == 0

    async def test_a_scope_refuses_to_end_or_revoke_another_hospitals_row(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        """A row loaded before the scope was bound is still another hospital's row."""
        now = datetime.now(UTC)
        [link] = await _rows(db_session, PatientAccountLink)
        [grant] = await _rows(db_session, PatientAccessGrant)
        [consent] = await _rows(db_session, PatientConsentRecord)

        async def _end() -> None:
            await PatientAccountLinkRepository(db_session).end_link(link, now=now, reason="x")

        async def _revoke() -> None:
            await PatientAccessGrantRepository(db_session).revoke(
                grant, now=now, account_id=shared.linked.account.id, reason=None
            )

        async def _withdraw() -> None:
            await PatientConsentRepository(db_session).withdraw(consent, now=now)

        for write in (_end, _revoke, _withdraw):
            with pytest.raises(CrossTenantAccessError):
                async with db_session.begin_nested():
                    with tenant_scope(TenantScope.hospital(shared.recipient.id)):
                        await write()

        [link] = await _rows(db_session, PatientAccountLink)
        [grant] = await _rows(db_session, PatientAccessGrant)
        [consent] = await _rows(db_session, PatientConsentRecord)
        assert (link.unlinked_at, grant.revoked_at, consent.withdrawn_at) == (None, None, None)

    async def test_a_row_cannot_be_moved_to_another_hospital(
        self, shared: _Shared, db_session: AsyncSession
    ) -> None:
        for model in (PatientAccountLink, PatientAccessGrant, PatientConsentRecord):
            [row] = await _rows(db_session, model)
            with pytest.raises(CrossTenantAccessError):
                async with db_session.begin_nested():
                    row.hospital_id = shared.recipient.id
                    await db_session.flush()

    async def test_an_accounts_own_links_are_the_only_cross_hospital_read_and_it_is_marked(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        await insert_patient_record(db_session, other_hospital.id, phone=linked.account.phone)
        assert await _link(db_session, linked.account, other_hospital.id)
        neighbour = await _linked(db_session, linked.hospital_id)
        links = PatientAccountLinkRepository(db_session)

        own = await links.list_active_for_account(linked.account.id, cross_tenant("own links"))
        here = await links.list_active_for_account(linked.account.id, linked.hospital_id)

        assert {link.hospital_id for link in own} == {linked.hospital_id, other_hospital.id}
        assert {link.account_id for link in own} == {linked.account.id}
        assert [link.hospital_id for link in here] == [linked.hospital_id]
        assert neighbour.account.id not in {link.account_id for link in own}
        with tenant_scope(TenantScope.hospital(other_hospital.id)):
            confined = await links.list_active_for_account(
                linked.account.id, cross_tenant("own links")
            )
        assert [link.hospital_id for link in confined] == [other_hospital.id]

    async def test_patient_flows_leave_no_scope_bound_whether_they_succeed_or_fail(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """A scope that outlived its request would confine — or free — the next one."""
        services = _services(db_session)
        stranger = await _new_account(db_session)
        flows: list[Callable[[], Awaitable[object]]] = [
            lambda: _link(db_session, linked.account, linked.hospital_id),
            lambda: _link(db_session, stranger, linked.hospital_id),
            lambda: services.authorization.resolve_context(linked.account, linked.hospital_id),
            lambda: services.authorization.resolve_context(stranger, linked.hospital_id),
            lambda: services.authorization.describe_links(linked.account),
            lambda: _share(db_session, linked, other_hospital.id),
            lambda: _authorize(db_session, Grantee(other_hospital.id), *linked.read),
            lambda: _authorize(db_session, Grantee(linked.hospital_id), *linked.read),
        ]

        for flow in flows:
            with contextlib.suppress(NotFoundError):
                await flow()
            assert current_tenant_scope() is None

    @pytest.mark.parametrize("repository", _TENANT_REPOSITORIES, ids=lambda r: r.__name__)
    def test_every_method_of_a_new_tenant_repository_takes_its_tenant(
        self, repository: type[BaseRepository[Any]]
    ) -> None:
        """Checked here independently of the structural guard in ``test_tenancy.py``."""
        from app.tests.unit.core.test_tenancy import EXPLICIT_EXCEPTIONS

        model_names = {mapper.class_.__name__ for mapper in Base.registry.mappers}
        assert issubclass(repository, BaseRepository)
        instance = repository(None)  # type: ignore[call-arg, arg-type]
        assert is_tenant_scoped(instance._model)  # noqa: SLF001 — the managed model

        methods = {
            name: list(inspect.signature(member).parameters.values())[1:]
            for name, member in vars(repository).items()
            if not name.startswith("_") and inspect.iscoroutinefunction(member)
        }

        assert methods
        for name, parameters in methods.items():
            takes_tenant = parameters[0].name in {"hospital_id", "scope"} or any(
                parameter.name == "scope" for parameter in parameters
            )
            acts_on_a_row = str(parameters[0].annotation) in model_names
            assert takes_tenant or acts_on_a_row, f"{repository.__name__}.{name}"
        # None of them needed an entry in the guard's list of exceptions.
        assert not [key for key in EXPLICIT_EXCEPTIONS if key.startswith(repository.__name__)]

    @pytest.mark.parametrize(
        ("repository", "model"), _PLATFORM_REPOSITORIES, ids=lambda value: value.__name__
    )
    def test_the_identity_repositories_hold_nothing_that_belongs_to_a_hospital(
        self, repository: type[Any], model: type[Any]
    ) -> None:
        """They sit outside the tenant guard only because their tables have no tenant data."""
        assert not issubclass(repository, BaseRepository)
        assert not is_tenant_scoped(model)
        columns = set(model.__table__.c.keys())
        assert not {name for name in columns if "hospital" in name or "patient_id" in name}
        referenced = {key.column.table.name for key in model.__table__.foreign_keys}
        assert not referenced & {"hospitals", "patients", "users"}


# ── 9. The record dependency confines the request to the link's hospital ─────


class TestThePatientContextBindsTheTenant:
    """``get_patient_context`` — what every record endpoint will be built on."""

    async def test_it_confines_the_request_to_the_hospital_of_the_accounts_own_link(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Attack: a record endpoint that reaches, by a bug, for another hospital's row.

        Behind the dependency the session's tenant guard is on: a read for
        another hospital's row finds nothing, and a flush that would write one
        is refused.
        """
        theirs = await insert_patient_record(db_session, other_hospital.id, phone=new_phone())
        # By value: the refused flush is rolled back, which expires loaded rows.
        theirs_id, mine_id = theirs.id, linked.patient.id
        mine_hospital, their_hospital = linked.hospital_id, other_hospital.id
        account = linked.account
        seen: dict[str, Any] = {}

        async def _request() -> None:
            context = await get_patient_context(
                mine_hospital, account, _services(db_session).authorization
            )
            seen["context"] = context
            seen["scope"] = current_tenant_scope()
            mine = await _rows(db_session, Patient, Patient.id == mine_id)
            seen["mine"] = [row.id for row in mine]
            # A query for the other hospital's row, by its id: filtered out.
            theirs_seen = await _rows(db_session, Patient, Patient.id == theirs_id)
            seen["theirs"] = [row.id for row in theirs_seen]
            seen["everything"] = {row.hospital_id for row in await _rows(db_session, Patient)}
            # A write to the other hospital: refused at flush.
            db_session.add(
                Patient(
                    id=uuid.uuid4(),
                    hospital_id=their_hospital,
                    mrn=f"MRN-X-{uuid.uuid4().hex[:8]}",
                    first_name="Planted",
                    last_name="Row",
                    date_of_birth=DOB,
                    gender=Gender.FEMALE,
                )
            )
            with pytest.raises(CrossTenantAccessError):
                await db_session.flush()
            await db_session.rollback()

        # Its own task, as a request is: what the dependency binds ends with it.
        await asyncio.create_task(_request())

        assert seen["scope"] == TenantScope.hospital(mine_hospital)
        assert seen["context"].hospital_id == mine_hospital
        assert seen["context"].patient_id == mine_id
        assert seen["mine"] == [mine_id]
        assert seen["theirs"] == []
        assert seen["everything"] == {mine_hospital}
        assert current_tenant_scope() is None
        assert await _count(db_session, Patient, Patient.first_name == "Planted") == 0

    async def test_the_scope_is_the_links_hospital_never_the_one_the_request_named(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Attack: name another hospital in the path to be confined to *it*."""
        seen: dict[str, Any] = {}

        async def _request() -> None:
            with pytest.raises(NotFoundError):
                await get_patient_context(
                    other_hospital.id, linked.account, _services(db_session).authorization
                )
            seen["scope"] = current_tenant_scope()

        await asyncio.create_task(_request())

        # Refused, and nothing was bound: the request ends there.
        assert seen["scope"] is None

    async def test_nothing_is_bound_when_a_consent_is_missing(
        self, db_session: AsyncSession, linked: _Linked
    ) -> None:
        seen: dict[str, Any] = {}

        async def _request() -> None:
            with pytest.raises(ConsentRequiredError):
                await get_patient_context(
                    linked.hospital_id,
                    linked.account,
                    _services(db_session, WITH_REQUIRED_TERMS).authorization,
                )
            seen["scope"] = current_tenant_scope()

        await asyncio.create_task(_request())

        assert seen["scope"] is None


# ── 10. No policy is ever accepted on a patient's behalf ─────────────────────

PLATFORM_PURPOSES = ("terms_of_service", "privacy_notice")


def _gated_application(session: AsyncSession, sms: FakeSmsSender) -> Any:
    """The real application, with one platform policy switched to required.

    Exactly what flipping ``required`` in ``policies.py`` will do: only the
    list of policies handed to :class:`ConsentService` differs.
    """
    application = build_patient_application(session, sms)

    def _consent(
        consents: PatientConsentRepository = Depends(get_patient_consent_repository),
        links: PatientAccountLinkRepository = Depends(get_patient_account_link_repository),
        uow: UnitOfWork = Depends(get_unit_of_work),
        audit: AuditSink = Depends(get_audit_sink),
    ) -> ConsentService:
        return ConsentService(consents, links, uow, audit, policies=WITH_REQUIRED_TERMS)

    application.dependency_overrides[get_consent_service] = _consent
    return application


async def _platform_consents(session: AsyncSession) -> list[PatientConsentRecord]:
    return await _rows(
        session, PatientConsentRecord, PatientConsentRecord.purpose.in_(PLATFORM_PURPOSES)
    )


class TestNoPolicyIsAcceptedSilently:
    """There is no official text yet, so nothing may record that one was accepted."""

    @pytest.fixture(autouse=True)
    def _plain_http(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
        monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
        monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)

    def test_no_platform_policy_is_required_today_and_every_text_is_a_draft(self) -> None:
        """Why ``pending_policies`` is empty: nothing is required — not because it was accepted."""
        platform = [policy for policy in CURRENT_POLICIES if policy.scope == "platform"]

        assert sorted(policy.purpose.value for policy in platform) == sorted(PLATFORM_PURPOSES)
        assert [policy.required for policy in platform] == [False, False]
        assert {policy.version for policy in CURRENT_POLICIES} == {DRAFT_VERSION}
        assert DRAFT_VERSION.endswith("-draft")

    def test_nothing_in_the_application_accepts_a_platform_policy(self) -> None:
        """A structural guard: no code path names a platform policy or calls ``accept``.

        The two purposes appear where they are defined and nowhere else, and
        ``ConsentService.accept`` — the only way to record one — has no caller
        in the application. A sign-in, a refresh, a link or a registration
        therefore cannot write such a consent, whatever it is given.
        """
        app_root = Path(access_grant_service.__file__).resolve().parents[2]
        defined_in = {
            app_root / "models" / "patient_consent.py",
            app_root / "services" / "patient_app" / "policies.py",
        }
        naming, accepting = [], []
        for path in sorted(app_root.rglob("*.py")):
            if (app_root / "tests") in path.parents:
                continue
            source = path.read_text(encoding="utf-8")
            # ``terms_of_service=`` in ``main.py`` is the OpenAPI document's
            # link to the public terms page, not a consent.
            named = re.sub(r"\bterms_of_service=", "", source)
            if path not in defined_in and re.search(
                r"TERMS_OF_SERVICE|PRIVACY_NOTICE|terms_of_service|privacy_notice", named
            ):
                naming.append(path.name)
            if re.search(r"\.accept\(", source):
                accepting.append(path.name)

        assert naming == []
        assert accepting == []
        assert all(path.exists() for path in defined_in)

    async def test_no_flow_writes_a_platform_consent(
        self, db_session: AsyncSession, hospital: Hospital, other_hospital: Hospital
    ) -> None:
        """Every flow a patient can drive, end to end: not one terms or privacy row."""
        sms = FakeSmsSender()
        application = build_patient_application(db_session, sms)
        phone = new_phone()
        await insert_patient_record(db_session, hospital.id, phone=phone)
        async with patient_client(application) as client:
            first = await sign_in(client, sms, phone)
            headers = bearer(first["access_token"])
            me = await client.get(f"{PATIENT}/me", headers=headers)
            linked_ = await client.post(
                f"{PATIENT}/hospitals/{hospital.id}/link",
                json={"date_of_birth": DOB.isoformat(), "consent_policy_version": POLICY},
                headers=headers,
            )
            registered = await client.post(
                f"{PATIENT}/hospitals/{other_hospital.id}/register",
                json={
                    "first_name": "Asha",
                    "last_name": "Verma",
                    "date_of_birth": DOB.isoformat(),
                    "gender": "female",
                    "consent_policy_version": POLICY,
                },
                headers=headers,
            )
            refreshed = await client.post(f"{PATIENT}/auth/refresh", headers=CSRF)
            headers = bearer(refreshed.json()["data"]["access_token"])
            again = await client.get(f"{PATIENT}/me", headers=headers)
            signed_out = await client.post(f"{PATIENT}/auth/logout-all", headers=headers)
            second = await sign_in(client, sms, phone)

        assert (linked_.status_code, registered.status_code) == (201, 201)
        assert (refreshed.status_code, signed_out.status_code) == (200, 204)
        # Nothing was pending at any point …
        assert first["pending_policies"] == second["pending_policies"] == []
        assert me.json()["data"]["pending_policies"] == []
        assert again.json()["data"]["pending_policies"] == []
        # … and not because something recorded an acceptance.
        assert await _platform_consents(db_session) == []
        consents = await _rows(db_session, PatientConsentRecord)
        assert sorted((row.purpose, row.hospital_id) for row in consents) == sorted(
            [
                ("hospital_record_link", hospital.id),
                ("hospital_record_link", other_hospital.id),
                ("hospital_registration", other_hospital.id),
            ]
        )
        assert all(row.hospital_id is not None for row in consents)
        granted = await audit_rows(db_session, "patient.consent.granted")
        assert {(row.context or {})["purpose"] for row in granted} == {
            "hospital_record_link",
            "hospital_registration",
        }

    async def test_a_required_policy_is_listed_as_pending_and_closes_the_gate(
        self, db_session: AsyncSession, hospital: Hospital
    ) -> None:
        """The day a text exists and is required: listed everywhere, refused everywhere.

        And still nothing accepts it for the patient — the row appears only
        when they accept, by the one explicit act.
        """
        sms = FakeSmsSender()
        application = _gated_application(db_session, sms)
        phone = new_phone()
        record = await insert_patient_record(db_session, hospital.id, phone=phone)
        record_id = record.id
        wanted = [{"purpose": "terms_of_service", "version": "2027-01"}]
        link_body = {"date_of_birth": DOB.isoformat(), "consent_policy_version": POLICY}
        register_body = {**link_body, "first_name": "Asha", "last_name": "Rao", "gender": "female"}
        async with patient_client(application) as client:
            session = await sign_in(client, sms, phone)
            headers = bearer(session["access_token"])
            account_id = uuid.UUID(session["account"]["id"])
            me = await client.get(f"{PATIENT}/me", headers=headers)
            refreshed = await client.post(f"{PATIENT}/auth/refresh", headers=CSRF)
            headers = bearer(refreshed.json()["data"]["access_token"])
            link = await client.post(
                f"{PATIENT}/hospitals/{hospital.id}/link", json=link_body, headers=headers
            )
            register = await client.post(
                f"{PATIENT}/hospitals/{hospital.id}/register", json=register_body, headers=headers
            )
            # The gate answers before the hospital is looked at: an invented
            # reference gets the same refusal, not a 404.
            nowhere = await client.post(
                f"{PATIENT}/hospitals/{uuid.uuid4()}/link", json=link_body, headers=headers
            )

            assert session["pending_policies"] == wanted
            assert me.json()["data"]["pending_policies"] == wanted
            for refused in (link, register, nowhere):
                assert refused.status_code == 403, refused.text
                assert refused.json()["error_code"] == "CONSENT_REQUIRED"
            assert link.json() | {"metadata": None} == nowhere.json() | {"metadata": None}
            # Refused whole: no link, no consent of any kind, no attempt charged.
            assert await _count(db_session, PatientAccountLink) == 0
            assert await _count(db_session, PatientConsentRecord) == 0
            assert await audit_rows(db_session, "patient.link.attempted") == []
            assert await _platform_consents(db_session) == []

            # The one explicit act.
            await _services(db_session, WITH_REQUIRED_TERMS).consent.accept(
                account_id, ConsentPurpose.TERMS_OF_SERVICE, policy_version="2027-01", client=CLIENT
            )
            after = await client.get(f"{PATIENT}/me", headers=headers)
            linked_now = await client.post(
                f"{PATIENT}/hospitals/{hospital.id}/link", json=link_body, headers=headers
            )

        assert after.json()["data"]["pending_policies"] == []
        assert linked_now.status_code == 201, linked_now.text
        [terms] = await _platform_consents(db_session)
        assert (terms.account_id, terms.policy_version, terms.hospital_id) == (
            account_id,
            "2027-01",
            None,
        )
        [link_row] = await _rows(db_session, PatientAccountLink)
        assert link_row.patient_id == record_id

    async def test_an_acceptance_of_the_draft_does_not_satisfy_the_real_text(
        self, db_session: AsyncSession
    ) -> None:
        """Attack: accept the draft marker today to be "already accepted" when the text lands."""
        account = await _new_account(db_session)
        today = _services(db_session)
        await today.consent.accept(
            account.id, ConsentPurpose.TERMS_OF_SERVICE, policy_version=DRAFT_VERSION
        )
        gated = _services(db_session, WITH_REQUIRED_TERMS)

        pending = await gated.consent.pending_policies(account.id)

        assert [(p.purpose, p.version) for p in pending] == [("terms_of_service", "2027-01")]
        with pytest.raises(ConsentRequiredError):
            await gated.consent.ensure_policies_accepted(account.id)


# ── 11. A grant belongs to the link it was given under ───────────────────────


class TestAGrantBelongsToItsLink:
    async def test_the_live_link_must_be_no_younger_than_the_grant(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """The rule itself, at its boundary: established at the grant's instant counts; after it, not."""
        await _share(db_session, linked, other_hospital.id)
        [grant] = await _rows(db_session, PatientAccessGrant)
        granted_at = grant.granted_at
        reader = Grantee(other_hospital.id)

        for linked_at, allowed in (
            (granted_at - timedelta(days=3), True),
            (granted_at, True),
            (granted_at + timedelta(microseconds=1), False),
            (granted_at + timedelta(days=3), False),
        ):
            await db_session.execute(update(PatientAccountLink).values(linked_at=linked_at))
            await db_session.commit()
            if allowed:
                assert (await _authorize(db_session, reader, *linked.read)).id == grant.id
            else:
                with pytest.raises(NotFoundError):
                    await _authorize(db_session, reader, *linked.read)

    async def test_a_grant_given_after_linking_again_works_and_the_old_one_stays_dead(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """Linking again is a clean start: what the patient shares *now* is what is shared."""
        old = await _share(db_session, linked, other_hospital.id)
        await _services(db_session).consent.withdraw(
            linked.account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=linked.hospital_id
        )
        assert await _link(db_session, linked.account, linked.hospital_id) is True
        with pytest.raises(NotFoundError):
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)

        new = await _share(db_session, linked, other_hospital.id)

        used = await _authorize(db_session, Grantee(other_hospital.id), *linked.read)
        assert used.id == new
        assert used.id != old
        # The old grant was not touched: it is unusable, not rewritten.
        [old_row] = await _rows(db_session, PatientAccessGrant, PatientAccessGrant.id == old)
        assert old_row.revoked_at is None

    async def test_confirming_the_same_link_again_does_not_end_its_grants(
        self, db_session: AsyncSession, linked: _Linked, other_hospital: Hospital
    ) -> None:
        """An idempotent re-link is the same link: its grants are still its grants."""
        granted = await _share(db_session, linked, other_hospital.id)

        assert await _link(db_session, linked.account, linked.hospital_id) is False

        assert (
            await _authorize(db_session, Grantee(other_hospital.id), *linked.read)
        ).id == granted
