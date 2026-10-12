"""Attacks on Patient App record linking and self-registration.

``docs/modules/15-patient-app.md`` §4.4, §4.5 and §7. Linking is where a
verified phone number is turned into access to a medical record, so every test
here names what somebody is trying to get and asserts that they do not get it:

* every row of the §4.5 outcome table, with what is left behind in the
  database and the audit trail;
* the answers that must not differ — wrong date of birth, no record, a
  hospital that cannot be used — compared whole: status, body and headers;
* identifiers a client tries to supply, in the body, the query string and
  headers;
* one hospital at a time: a record of hospital B is never reached through
  hospital A;
* the attempt allowance, per account and hospital;
* links that went stale, and the phone-binding rule;
* self-registration;
* the same things under real concurrency.

Accounts are created directly and given a patient access token: how a patient
proves a phone number has its own suite. Everything else goes over HTTP,
through the real application, against a real PostgreSQL.

The concurrency tests cannot use the rolled-back ``db_session``: on one
connection two requests are never inside a transaction at once, an advisory
lock never has anyone to wait for and a unique index never sees a competing
insert. They use independent connections with real commits, and delete every
row they wrote in ``finally``.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.api.dependencies.patient import get_sms_sender
from app.core.config import settings
from app.core.exceptions import NotFoundError, ValidationError
from app.core.feature_flags import PATIENT_APP_ENABLED
from app.core.security import create_patient_access_token
from app.database.unit_of_work import UnitOfWork
from app.main import create_app
from app.models.audit_log import AuditLog
from app.models.auth_throttle import AuthThrottleBucket
from app.models.hospital import Hospital
from app.models.patient import Gender, MrnSequence, Patient
from app.models.patient_account import PatientAccount, PatientAccountLink
from app.models.patient_consent import (
    ConsentPurpose,
    GranteeType,
    GrantPurposeNote,
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
from app.repositories.auth_throttle_repository import AuthThrottleRepository
from app.schemas.patient_app.account import RegisterRecordRequest
from app.services.audit_service import AuditService
from app.services.auth_throttle import AuthThrottle, BucketKind, bucket
from app.services.mrn_service import MRNService
from app.services.patient_app.access_grant_service import AccessGrantService, Grantee
from app.services.patient_app.common import ClientContext
from app.services.patient_app.consent_service import ConsentService
from app.services.patient_app.hospital_gate import PatientHospitalGate
from app.services.patient_app.patient_authorization import PatientAuthorization
from app.services.patient_app.record_link_service import RecordLinkService
from app.services.patient_service import PatientService
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
    ThrottleClock,
    audit_rows,
    bearer,
    build_patient_application,
    insert_patient_record,
    new_phone,
    open_hospital,
    patient_client,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncEngine

pytestmark = pytest.mark.database

ME = f"{PATIENT}/me"
DOB = date(1990, 5, 17)
OTHER_DOB = date(1985, 1, 2)
NO_MATCH = "We could not find a record with these details."
CONTACT_HOSPITAL = "Please contact the hospital."

# Documented number (PT_LINK_ATTEMPT: burst 5, one back every 12 minutes),
# restated on purpose.
LINK_ATTEMPTS = 5

#: Response headers that differ between any two requests, whatever was asked:
#: the request's id, how long it took, and the caller's own request counter.
_PER_REQUEST_HEADERS = frozenset(
    {"x-request-id", "x-response-time", "x-ratelimit-remaining", "x-ratelimit-reset"}
)


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)


# ── A patient, and what they can ask ─────────────────────────────────────────


class _Caller:
    """A signed-in patient: an account, its verified phone and a browser."""

    def __init__(self, client: AsyncClient, account_id: uuid.UUID, phone: str) -> None:
        self.client = client
        self.account_id = account_id
        self.phone = phone
        self.headers = bearer(create_patient_access_token(account_id))

    async def link(
        self,
        hospital_ref: object,
        *,
        params: dict[str, str] | None = None,
        extra_headers: dict[str, str] | None = None,
        **body: Any,
    ) -> Response:
        payload = {"date_of_birth": DOB.isoformat(), "consent_policy_version": POLICY, **body}
        return await self.client.post(
            f"{PATIENT}/hospitals/{hospital_ref}/link",
            json=payload,
            params=params,
            headers={**self.headers, **(extra_headers or {})},
        )

    async def register(self, hospital_ref: object, **body: Any) -> Response:
        payload = {
            "first_name": "Asha",
            "last_name": "Verma",
            "date_of_birth": DOB.isoformat(),
            "gender": "female",
            "consent_policy_version": POLICY,
            **body,
        }
        return await self.client.post(
            f"{PATIENT}/hospitals/{hospital_ref}/register", json=payload, headers=self.headers
        )

    async def links(self) -> list[dict[str, Any]]:
        response = await self.client.get(ME, headers=self.headers)
        assert response.status_code == 200, response.text
        links: list[dict[str, Any]] = response.json()["data"]["links"]
        return links


async def _enrol(session: AsyncSession, client: AsyncClient, phone: str | None = None) -> _Caller:
    """An active account on a verified phone, as a successful sign-in leaves it."""
    account = PatientAccount(
        id=uuid.uuid4(),
        phone=phone or new_phone(),
        status="active",
        phone_verified_at=datetime.now(UTC),
    )
    session.add(account)
    await session.flush()
    await session.commit()
    return _Caller(client, account.id, account.phone)


@pytest_asyncio.fixture
async def application(db_session: AsyncSession) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, None)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


@pytest_asyncio.fixture
async def other_browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    async with patient_client(application) as client:
        yield client


@pytest_asyncio.fixture
async def hospital(db_session: AsyncSession, hospital_id: uuid.UUID) -> Hospital:
    """Hospital A, open to the Patient App."""
    return await open_hospital(db_session, hospital_id)


@pytest_asyncio.fixture
async def other_hospital(db_session: AsyncSession, other_hospital_id: uuid.UUID) -> Hospital:
    """Hospital B, open to the Patient App."""
    return await open_hospital(db_session, other_hospital_id)


@pytest_asyncio.fixture
async def patient(db_session: AsyncSession, browser: AsyncClient) -> _Caller:
    return await _enrol(db_session, browser)


@pytest_asyncio.fixture
async def stranger(db_session: AsyncSession, other_browser: AsyncClient) -> _Caller:
    """A second account, on another phone, in its own browser."""
    return await _enrol(db_session, other_browser)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    return ThrottleClock(monkeypatch)


# ── Reading what was left behind ─────────────────────────────────────────────


async def _active_links(
    session: AsyncSession,
    *,
    account_id: uuid.UUID | None = None,
    patient_id: uuid.UUID | None = None,
) -> list[PatientAccountLink]:
    stmt = select(PatientAccountLink).where(PatientAccountLink.unlinked_at.is_(None))
    if account_id is not None:
        stmt = stmt.where(PatientAccountLink.account_id == account_id)
    if patient_id is not None:
        stmt = stmt.where(PatientAccountLink.patient_id == patient_id)
    result = await session.execute(stmt.execution_options(populate_existing=True))
    return list(result.scalars().all())


async def _attempts(session: AsyncSession, account_id: uuid.UUID) -> list[AuditLog]:
    rows = await audit_rows(session, "patient.link.attempted")
    return [row for row in rows if row.patient_account_id == account_id]


async def _outcomes(session: AsyncSession, account_id: uuid.UUID) -> Counter[str]:
    return Counter(
        str((row.context or {})["outcome"]) for row in await _attempts(session, account_id)
    )


async def _records_on(session: AsyncSession, phone: str) -> list[Patient]:
    result = await session.execute(
        select(Patient).where(Patient.phone == phone).execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _count(session: AsyncSession, model: Any, *criteria: Any) -> int:
    result = await session.execute(select(func.count()).select_from(model).where(*criteria))
    return int(result.scalar_one())


async def _move_record_to(session: AsyncSession, record_id: uuid.UUID, phone: str) -> None:
    """What hospital staff do when they correct the phone on a record."""
    await session.execute(update(Patient).where(Patient.id == record_id).values(phone=phone))
    await session.commit()


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _wire(response: Response) -> tuple[int, dict[str, Any], dict[str, str]]:
    """Everything a caller can see of an answer, less what differs on every request."""
    body: dict[str, Any] = response.json()
    body.pop("metadata", None)
    headers = {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in _PER_REQUEST_HEADERS
    }
    return response.status_code, body, headers


def _shape(row: AuditLog) -> dict[str, Any]:
    """An audit row, less who and when."""
    return {
        "action": row.action,
        "actor_type": row.actor_type,
        "actor_user_id": row.actor_user_id,
        "hospital_id": row.hospital_id,
        "target_type": row.target_type,
        "target_id": row.target_id,
        "context": row.context,
    }


def _stored(row: AuditLog) -> str:
    """Everything an audit row holds, as text, to search for what must not be there."""
    return json.dumps(
        {column.name: str(getattr(row, column.name)) for column in AuditLog.__table__.columns}
        | {"context": row.context}
    )


# ── 1. The outcome table of §4.5, row by row ─────────────────────────────────


@dataclass
class _World:
    session: AsyncSession
    hospital: Hospital
    patient: _Caller
    stranger: _Caller


async def _no_match(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=new_phone())
    return await world.patient.link(world.hospital.id)


async def _wrong_date_of_birth(world: _World) -> Response:
    await insert_patient_record(
        world.session, world.hospital.id, phone=world.patient.phone, date_of_birth=OTHER_DOB
    )
    return await world.patient.link(world.hospital.id)


async def _unique_match(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    return await world.patient.link(world.hospital.id)


async def _ambiguous_match(world: _World) -> Response:
    for _ in range(2):
        await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    return await world.patient.link(world.hospital.id)


async def _ambiguous_with_the_mrn(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    mine = await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    return await world.patient.link(world.hospital.id, mrn=mine.mrn)


async def _ambiguous_with_a_wrong_mrn(world: _World) -> Response:
    for _ in range(2):
        await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    return await world.patient.link(world.hospital.id, mrn="MRN-NOT-MINE")


async def _attempts_used_up(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    for day in range(LINK_ATTEMPTS):
        guess = (OTHER_DOB + timedelta(days=day)).isoformat()
        await world.patient.link(world.hospital.id, date_of_birth=guess)
    return await world.patient.link(world.hospital.id)


async def _inactive_record(world: _World) -> Response:
    await insert_patient_record(
        world.session, world.hospital.id, phone=world.patient.phone, deleted=True
    )
    return await world.patient.link(world.hospital.id)


async def _already_linked(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    await world.patient.link(world.hospital.id)
    return await world.patient.link(world.hospital.id)


async def _linked_to_another_record_here(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    await insert_patient_record(
        world.session, world.hospital.id, phone=world.patient.phone, date_of_birth=OTHER_DOB
    )
    await world.patient.link(world.hospital.id)
    return await world.patient.link(world.hospital.id, date_of_birth=OTHER_DOB.isoformat())


async def _linked_by_another_account(world: _World) -> Response:
    record = await insert_patient_record(
        world.session, world.hospital.id, phone=world.stranger.phone
    )
    await world.stranger.link(world.hospital.id)
    await _move_record_to(world.session, record.id, world.patient.phone)
    return await world.patient.link(world.hospital.id)


async def _flag_off(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    await open_hospital(world.session, world.hospital.id, enabled=False)
    return await world.patient.link(world.hospital.id)


async def _hospital_inactive(world: _World) -> Response:
    await insert_patient_record(world.session, world.hospital.id, phone=world.patient.phone)
    await world.session.execute(
        update(Hospital).where(Hospital.id == world.hospital.id).values(is_active=False)
    )
    await world.session.commit()
    return await world.patient.link(world.hospital.id)


#: scene → (status, error code, active links of the account afterwards, audited outcomes).
_TABLE: dict[
    str, tuple[Callable[[_World], Awaitable[Response]], int, str | None, int, dict[str, int]]
] = {
    "no match": (_no_match, 404, "RESOURCE_NOT_FOUND", 0, {"no_match": 1}),
    "wrong date of birth": (_wrong_date_of_birth, 404, "RESOURCE_NOT_FOUND", 0, {"no_match": 1}),
    "unique match": (_unique_match, 201, None, 1, {"linked": 1}),
    "ambiguous match": (_ambiguous_match, 409, "LINK_MRN_REQUIRED", 0, {"mrn_required": 1}),
    "ambiguous, MRN supplied": (_ambiguous_with_the_mrn, 201, None, 1, {"linked": 1}),
    "ambiguous, MRN wrong": (
        _ambiguous_with_a_wrong_mrn,
        404,
        "RESOURCE_NOT_FOUND",
        0,
        {"no_match": 1},
    ),
    "still unresolved": (
        _attempts_used_up,
        403,
        "LINK_UNAVAILABLE",
        0,
        {"no_match": LINK_ATTEMPTS, "throttled": 1},
    ),
    "inactive patient": (_inactive_record, 403, "LINK_UNAVAILABLE", 0, {"inactive_record": 1}),
    "already linked, same account": (
        _already_linked,
        200,
        None,
        1,
        {"linked": 1, "already_linked": 1},
    ),
    "already linked, other record": (
        _linked_to_another_record_here,
        409,
        "RESOURCE_CONFLICT",
        1,
        {"linked": 1, "conflict": 1},
    ),
    "already linked, another account": (_linked_by_another_account, 201, None, 1, {"linked": 1}),
    "hospital flag off": (_flag_off, 404, "RESOURCE_NOT_FOUND", 0, {"hospital_unavailable": 1}),
    "hospital inactive": (
        _hospital_inactive,
        404,
        "RESOURCE_NOT_FOUND",
        0,
        {"hospital_unavailable": 1},
    ),
}


class TestOutcomeTable:
    """Every row of §4.5: the answer, what it leaves in the database, and its audit entry."""

    @pytest.mark.parametrize("row", list(_TABLE))
    async def test_each_row_answers_as_the_table_says(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        row: str,
    ) -> None:
        scene, status, code, links, outcomes = _TABLE[row]

        response = await scene(_World(db_session, hospital, patient, stranger))

        assert response.status_code == status, response.text
        assert response.json().get("error_code") == code
        assert len(await _active_links(db_session, account_id=patient.account_id)) == links
        assert await _outcomes(db_session, patient.account_id) == Counter(outcomes)

    @pytest.mark.parametrize("row", list(_TABLE))
    async def test_no_row_puts_what_the_patient_typed_into_the_audit_trail(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        row: str,
    ) -> None:
        """The audit row never holds the entered date of birth, an MRN or a phone number."""
        await _TABLE[row][0](_World(db_session, hospital, patient, stranger))

        mrns = [record.mrn for record in await _records_on(db_session, patient.phone)]
        attempts = await _attempts(db_session, patient.account_id)
        assert attempts
        for attempt in attempts:
            assert attempt.actor_type == "patient"
            assert attempt.actor_user_id is None
            assert set(attempt.context or {}) == {"operation", "outcome", "mrn_supplied"}
            stored = _stored(attempt)
            for secret in (
                DOB.isoformat(),
                OTHER_DOB.isoformat(),
                "MRN-NOT-MINE",
                patient.phone,
                stranger.phone,
                *mrns,
            ):
                assert secret not in stored

    @pytest.mark.parametrize("row", [name for name, entry in _TABLE.items() if entry[1] >= 400])
    async def test_a_refusal_describes_no_record(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        row: str,
    ) -> None:
        """The API never returns, counts or describes candidate records."""
        response = await _TABLE[row][0](_World(db_session, hospital, patient, stranger))

        assert set(response.json()) == {"success", "message", "error_code", "errors", "metadata"}
        assert not response.json()["errors"]
        for record in await _records_on(db_session, patient.phone):
            for secret in (str(record.id), record.mrn, record.first_name, record.last_name):
                assert secret not in response.text
        assert patient.phone not in response.text


# ── 2. Answers that must not differ ──────────────────────────────────────────


class TestTheAnswerNeverSaysWhy:
    @pytest.mark.parametrize("with_mrn", [False, True])
    async def test_a_wrong_date_of_birth_and_no_record_are_one_answer(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        with_mrn: bool,
    ) -> None:
        """Attack: learn that a phone is on somebody's record by trying dates of birth.

        One caller's phone is on a record (and they even know its MRN); the
        other's is on none. Status, body, headers and audit entry are the same.
        """
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        body: dict[str, Any] = {"date_of_birth": OTHER_DOB.isoformat()}
        if with_mrn:
            body["mrn"] = record.mrn

        on_a_record = await patient.link(hospital.id, **body)
        on_no_record = await stranger.link(hospital.id, **body)

        assert on_a_record.status_code == 404
        assert _wire(on_a_record) == _wire(on_no_record)
        assert on_a_record.content.replace(
            on_a_record.json()["metadata"]["request_id"].encode(), b""
        ) == on_no_record.content.replace(
            on_no_record.json()["metadata"]["request_id"].encode(), b""
        )
        [wrong] = await _attempts(db_session, patient.account_id)
        [absent] = await _attempts(db_session, stranger.account_id)
        assert _shape(wrong) == _shape(absent)
        assert wrong.context == {
            "operation": "link",
            "outcome": "no_match",
            "mrn_supplied": with_mrn,
        }
        assert await _active_links(db_session) == []

    @pytest.mark.parametrize("state", ["flag_off", "flag_absent", "inactive", "unknown", "garbage"])
    @pytest.mark.parametrize("operation", ["link", "register"])
    async def test_a_hospital_that_cannot_be_used_is_the_answer_for_no_record(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
        patient: _Caller,
        state: str,
        operation: str,
    ) -> None:
        """Attack: find out which hospitals exist, or hold a record on this phone."""
        await insert_patient_record(db_session, other_hospital_id, phone=patient.phone)
        reference: object = other_hospital_id
        if state == "flag_off":
            await open_hospital(db_session, other_hospital_id, enabled=False)
        elif state == "inactive":
            await open_hospital(db_session, other_hospital_id)
            await db_session.execute(
                update(Hospital).where(Hospital.id == other_hospital_id).values(is_active=False)
            )
            await db_session.commit()
        elif state == "unknown":
            reference = uuid.uuid4()
        elif state == "garbage":
            reference = "no such hospital!"
        no_record = await patient.link(hospital.id)

        if operation == "link":
            unusable = await patient.link(reference)
        else:
            unusable = await patient.register(reference)

        assert _error(no_record) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert _wire(unusable) == _wire(no_record)
        assert await _active_links(db_session, account_id=patient.account_id) == []
        # And nothing was created at the hospital that cannot be used.
        assert len(await _records_on(db_session, patient.phone)) == 1
        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_a_stale_consent_version_is_refused_the_same_with_or_without_a_record(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: use the consent check, which needs no date of birth, as the oracle."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        on_a_record = await patient.link(hospital.id, consent_policy_version="2025-01")
        on_no_record = await stranger.link(hospital.id, consent_policy_version="2025-01")

        assert on_a_record.status_code == 409
        assert _wire(on_a_record) == _wire(on_no_record)
        assert await _active_links(db_session) == []
        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_an_mrn_alone_confirms_nothing(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: test MRNs against the API. The MRN is a tie-breaker and nothing else."""
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        # Not on the phone: a real MRN and an invented one are the same answer.
        real = await stranger.link(hospital.id, mrn=record.mrn)
        invented = await stranger.link(hospital.id, mrn="MRN-INVENTED")
        # On the phone, with the date of birth: the MRN is not even looked at.
        mine = await patient.link(hospital.id, mrn="MRN-INVENTED")

        assert _wire(real) == _wire(invented)
        assert real.status_code == 404
        assert mine.status_code == 201

    async def test_an_ambiguous_match_says_only_that_an_mrn_is_needed(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """Two candidates and three candidates are one answer."""
        for _ in range(2):
            await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        two = await patient.link(hospital.id)
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        three = await patient.link(hospital.id)

        assert two.status_code == 409
        assert _wire(two) == _wire(three)

    async def test_running_out_of_attempts_and_a_deactivated_record_are_one_answer(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone, deleted=True)
        for _ in range(LINK_ATTEMPTS):
            await stranger.link(hospital.id)

        deactivated = await patient.link(hospital.id)
        out_of_attempts = await stranger.link(hospital.id)

        assert _error(deactivated) == (403, "LINK_UNAVAILABLE", CONTACT_HOSPITAL)
        assert _wire(deactivated) == _wire(out_of_attempts)


# ── 3. Claiming somebody else's record ───────────────────────────────────────


class TestClaimingSomebodyElsesRecord:
    @pytest.mark.parametrize("field", ["patient_id", "phone", "hospital_id", "account_id"])
    async def test_an_identifier_in_the_body_is_refused_outright(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        field: str,
    ) -> None:
        """Attack: name the victim's record, phone or account in the request."""
        victim = await insert_patient_record(db_session, hospital.id, phone=stranger.phone)
        value = {
            "patient_id": str(victim.id),
            "phone": stranger.phone,
            "hospital_id": str(other_hospital.id),
            "account_id": str(stranger.account_id),
        }[field]

        smuggled: dict[str, Any] = {field: value}
        linked = await patient.link(hospital.id, mrn=victim.mrn, **smuggled)
        registered = await patient.register(hospital.id, **smuggled)

        assert linked.status_code == registered.status_code == 422
        assert await _active_links(db_session) == []
        assert await _records_on(db_session, patient.phone) == []
        assert [record.id for record in await _records_on(db_session, stranger.phone)] == [
            victim.id
        ]

    async def test_an_identifier_in_the_query_string_or_a_header_changes_nothing(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: the same, where a strict body schema does not look."""
        victim = await insert_patient_record(db_session, hospital.id, phone=stranger.phone)

        response = await patient.link(
            hospital.id,
            mrn=victim.mrn,
            params={
                "patient_id": str(victim.id),
                "phone": stranger.phone,
                "account_id": str(stranger.account_id),
            },
            extra_headers={
                "X-Patient-Id": str(victim.id),
                "X-Patient-Phone": stranger.phone,
                "X-Hospital-Id": str(hospital.id),
            },
        )

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _active_links(db_session) == []

    async def test_knowing_a_strangers_date_of_birth_and_mrn_is_not_enough(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: a relative, or a clerk, who knows everything on the card but the phone."""
        victim = await insert_patient_record(db_session, hospital.id, phone=stranger.phone)

        response = await patient.link(hospital.id, mrn=victim.mrn)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _active_links(db_session) == []

    async def test_a_strangers_mrn_never_breaks_a_tie_in_the_attackers_favour(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: hold two records of your own, so that an MRN is asked for — then give theirs."""
        for _ in range(2):
            await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        victim = await insert_patient_record(db_session, hospital.id, phone=stranger.phone)

        response = await patient.link(hospital.id, mrn=victim.mrn)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _active_links(db_session, patient_id=victim.id) == []
        assert await _active_links(db_session, account_id=patient.account_id) == []

    async def test_a_live_link_is_never_displaced_by_another_account(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: take over a record its owner has already linked."""
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        assert (await patient.link(hospital.id)).status_code == 201

        attempt = await stranger.link(hospital.id, mrn=record.mrn)
        registered = await stranger.register(hospital.id)

        assert attempt.status_code == 404
        assert registered.status_code == 201  # a record of their own, on their own phone
        [link] = await _active_links(db_session, patient_id=record.id)
        assert link.account_id == patient.account_id
        assert await audit_rows(db_session, "patient.link.ended") == []
        [theirs] = await _active_links(db_session, account_id=stranger.account_id)
        assert theirs.patient_id != record.id

    async def test_registering_with_a_strangers_details_creates_a_separate_record(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: register under the victim's name and date of birth to be merged into them."""
        victim = await insert_patient_record(
            db_session, hospital.id, phone=stranger.phone, first_name="Asha", last_name="Verma"
        )

        response = await patient.register(hospital.id)

        assert response.status_code == 201, response.text
        [mine] = await _records_on(db_session, patient.phone)
        assert mine.id != victim.id
        assert response.json()["data"]["profile"]["mrn"] == mine.mrn != victim.mrn
        assert await _active_links(db_session, patient_id=victim.id) == []


# ── 4. One hospital at a time ────────────────────────────────────────────────


class TestOneHospitalAtATime:
    @pytest.mark.parametrize("by", ["id", "slug"])
    async def test_a_record_of_hospital_b_is_never_reached_through_hospital_a(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
        by: str,
    ) -> None:
        """Attack: the patient's own phone and date of birth, at the wrong hospital."""
        theirs = await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        reference = hospital.id if by == "id" else hospital.slug

        plain = await patient.link(reference)
        with_their_mrn = await patient.link(reference, mrn=theirs.mrn)
        with_b_in_the_query = await patient.link(
            reference, params={"hospital_id": str(other_hospital.id)}
        )
        with_b_in_the_body = await patient.link(reference, hospital_id=str(other_hospital.id))

        for response in (plain, with_their_mrn, with_b_in_the_query):
            assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert with_b_in_the_body.status_code == 422
        assert await _active_links(db_session) == []

    async def test_what_happens_at_hospital_a_writes_nothing_at_hospital_b(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
    ) -> None:
        """A link, a refusal and a registration at A leave no row that names B."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        before = await _count(db_session, Patient, Patient.hospital_id == other_hospital.id)

        assert (await patient.link(hospital.id)).status_code == 201
        assert (await patient.link(hospital.id, date_of_birth="1970-01-01")).status_code == 404
        assert (await stranger.link(hospital.id)).status_code == 404
        assert (await stranger.register(hospital.id)).status_code == 201

        there = other_hospital.id
        assert await _count(db_session, PatientAccountLink) == 2
        assert (
            await _count(db_session, PatientAccountLink, PatientAccountLink.hospital_id == there)
            == 0
        )
        assert (
            await _count(
                db_session, PatientConsentRecord, PatientConsentRecord.hospital_id == there
            )
            == 0
        )
        assert await _count(db_session, AuditLog, AuditLog.hospital_id == there) == 0
        assert await _count(db_session, Patient, Patient.hospital_id == there) == before
        assert await _count(db_session, MrnSequence, MrnSequence.hospital_id == there) == 0
        assert {link["hospital_id"] for link in await patient.links()} == {str(hospital.id)}

    async def test_every_row_a_link_writes_names_the_hospital_in_the_path(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        await patient.link(hospital.slug)

        [link] = await _active_links(db_session)
        assert (link.hospital_id, link.patient_id, link.account_id) == (
            hospital.id,
            record.id,
            patient.account_id,
        )
        consents = (await db_session.execute(select(PatientConsentRecord))).scalars().all()
        assert [consent.hospital_id for consent in consents] == [hospital.id]
        events = [
            row
            for row in (await db_session.execute(select(AuditLog))).scalars().all()
            if row.patient_account_id == patient.account_id
        ]
        assert {row.action for row in events} == {
            "patient.link.attempted",
            "patient.link.created",
            "patient.consent.granted",
        }
        assert {row.hospital_id for row in events} == {hospital.id}

    async def test_another_spelling_of_a_hospital_reference_never_reaches_another_hospital(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
    ) -> None:
        """Attack: confuse the resolver with a reference that is nearly hospital B's."""
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        b = other_hospital.id

        for reference in (
            b.hex,  # no dashes
            f"{{{b}}}",
            f"urn:uuid:{b}",
            f"{hospital.id},{b}",
            f"{other_hospital.slug}%00",
            f"{other_hospital.slug}_",
        ):
            response = await patient.link(reference)
            assert response.status_code in (404, 422), (reference, response.text)
            if response.status_code == 404:
                assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _active_links(db_session) == []

        # The same hospital in capitals is still that hospital, and no other.
        assert (await patient.link(str(b).upper())).status_code == 201
        [link] = await _active_links(db_session)
        assert link.hospital_id == b

    async def test_a_registration_creates_a_record_in_that_hospital_only(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
    ) -> None:
        assert (await patient.register(hospital.slug)).status_code == 201

        [record] = await _records_on(db_session, patient.phone)
        assert record.hospital_id == hospital.id
        # Having registered at A is no reason to be refused, or matched, at B.
        assert (await patient.link(other_hospital.id)).status_code == 404
        assert (await patient.register(other_hospital.id)).status_code == 201
        assert {r.hospital_id for r in await _records_on(db_session, patient.phone)} == {
            hospital.id,
            other_hospital.id,
        }


# ── 5. The attempt allowance ─────────────────────────────────────────────────


class TestAttemptAllowance:
    async def test_once_it_is_used_up_the_right_details_are_refused_too(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, clock: ThrottleClock
    ) -> None:
        """Attack: work through dates of birth for a phone you hold.

        After five wrong guesses the sixth answer is the same whether the
        guess is right or wrong, so guessing on tells the attacker nothing.
        """
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        for day in range(LINK_ATTEMPTS):
            guess = (OTHER_DOB + timedelta(days=day)).isoformat()
            assert (await patient.link(hospital.id, date_of_birth=guess)).status_code == 404

        right = await patient.link(hospital.id)
        wrong = await patient.link(hospital.id, date_of_birth="1970-01-01")

        assert _error(right) == (403, "LINK_UNAVAILABLE", CONTACT_HOSPITAL)
        assert _wire(right) == _wire(wrong)
        assert await _active_links(db_session) == []
        # One attempt comes back every twelve minutes — one, not five.
        clock.advance(minutes=12, seconds=1)
        assert (await patient.link(hospital.id, date_of_birth="1970-01-02")).status_code == 404
        assert (await patient.link(hospital.id)).status_code == 403

    async def test_it_belongs_to_one_account(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: lock every patient of a hospital out of linking by failing there yourself."""
        await insert_patient_record(db_session, hospital.id, phone=stranger.phone)
        for _ in range(LINK_ATTEMPTS + 2):
            await patient.link(hospital.id)
        assert (await patient.link(hospital.id)).status_code == 403

        assert (await stranger.link(hospital.id)).status_code == 201

    async def test_it_belongs_to_one_hospital(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
    ) -> None:
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        for _ in range(LINK_ATTEMPTS):
            await patient.link(hospital.id)
        assert (await patient.link(hospital.slug)).status_code == 403  # id or code: one hospital

        assert (await patient.link(other_hospital.id)).status_code == 201

    async def test_linking_and_registering_draw_on_the_same_allowance(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """Attack: when linking is exhausted, keep probing through registration."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        for _ in range(LINK_ATTEMPTS):
            # Each is refused because a record matches — which is itself an answer.
            assert (await patient.register(hospital.id)).status_code == 409

        assert _error(await patient.register(hospital.id))[:2] == (403, "LINK_UNAVAILABLE")
        assert _error(await patient.link(hospital.id))[:2] == (403, "LINK_UNAVAILABLE")
        assert len(await _records_on(db_session, patient.phone)) == 1

    async def test_requests_refused_before_the_match_do_not_use_it_up(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """A malformed body, a stale consent version or an unknown hospital costs nothing."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        for _ in range(LINK_ATTEMPTS + 1):
            assert (await patient.link(hospital.id, date_of_birth="nonsense")).status_code == 422
            assert (
                await patient.link(hospital.id, consent_policy_version="old")
            ).status_code == 409
            assert (await patient.link(uuid.uuid4())).status_code == 404

        assert (await patient.link(hospital.id)).status_code == 201

    async def test_every_refused_attempt_is_audited_including_the_throttled_ones(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        for _ in range(LINK_ATTEMPTS + 3):
            await patient.link(hospital.id)

        assert await _outcomes(db_session, patient.account_id) == Counter(
            {"no_match": LINK_ATTEMPTS, "throttled": 3}
        )
        assert {row.hospital_id for row in await _attempts(db_session, patient.account_id)} == {
            hospital.id
        }


# ── 6. Links that went stale, and the phone-binding rule ─────────────────────


def _standing(session: AsyncSession) -> tuple[PatientAuthorization, AccessGrantService]:
    """The services a record endpoint will ask, composed as the providers compose them."""
    uow = UnitOfWork(session)
    audit = AuditService(session, AuditLogRepository(session), UserRepository(session))
    links = PatientAccountLinkRepository(session)
    hospitals = HospitalRepository(session)
    consent = ConsentService(PatientConsentRepository(session), links, uow, audit)
    patients = PatientService(
        PatientRepository(session), MRNService(MrnSequenceRepository(session)), session, audit
    )
    authorization = PatientAuthorization(
        PatientAccountRepository(session), links, PatientHospitalGate(hospitals), patients, consent
    )
    grants = AccessGrantService(
        PatientAccessGrantRepository(session),
        authorization,
        hospitals,
        DoctorRepository(session),
        uow=uow,
        audit=audit,
    )
    return authorization, grants


async def _account(session: AsyncSession, account_id: uuid.UUID) -> PatientAccount:
    result = await session.execute(
        select(PatientAccount)
        .where(PatientAccount.id == account_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


async def _share(
    session: AsyncSession, caller: _Caller, source_hospital_id: uuid.UUID, recipient: uuid.UUID
) -> uuid.UUID:
    _authorization, grants = _standing(session)
    view = await grants.create(
        await _account(session, caller.account_id),
        source_hospital_id,
        grantee_type=GranteeType.HOSPITAL,
        grantee_id=recipient,
        grantee_hospital_id=recipient,
        categories=[RecordCategory.PRESCRIPTIONS],
        purpose_note=GrantPurposeNote.CONSULTATION,
    )
    return view.id


class TestStaleLinks:
    async def test_the_new_owner_of_a_number_replaces_the_old_owners_link(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
    ) -> None:
        """The hospital moved a record to a new number; the old number's owner keeps nothing.

        Not their link, not a context to read records with, and not the
        sharing they set up while the record was theirs.
        """
        old_owner, new_owner = stranger, patient
        record = await insert_patient_record(db_session, hospital.id, phone=old_owner.phone)
        assert (await old_owner.link(hospital.id)).status_code == 201
        await _share(db_session, old_owner, hospital.id, other_hospital.id)
        await _move_record_to(db_session, record.id, new_owner.phone)

        response = await new_owner.link(hospital.id)

        assert response.status_code == 201, response.text
        [fresh] = await _active_links(db_session, patient_id=record.id)
        assert fresh.account_id == new_owner.account_id
        assert await _active_links(db_session, account_id=old_owner.account_id) == []
        assert await old_owner.links() == []

        [ended] = await audit_rows(db_session, "patient.link.ended")
        assert ended.patient_account_id == new_owner.account_id
        assert ended.hospital_id == hospital.id
        assert ended.context == {
            "reason": "superseded",
            "previous_account_id": str(old_owner.account_id),
        }
        created = await audit_rows(db_session, "patient.link.created")
        assert {row.target_id for row in created} >= {fresh.id}

        authorization, grants = _standing(db_session)
        with pytest.raises(NotFoundError):
            await authorization.resolve_context(
                await _account(db_session, old_owner.account_id), hospital.id
            )
        with pytest.raises(NotFoundError):
            await grants.authorize(
                Grantee(other_hospital.id),
                record.id,
                hospital.id,
                RecordCategory.PRESCRIPTIONS,
                DOB,
            )

    async def test_the_old_owner_cannot_take_the_record_back(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: the previous holder of the number links again once it has moved on."""
        old_owner, new_owner = stranger, patient
        record = await insert_patient_record(db_session, hospital.id, phone=old_owner.phone)
        await old_owner.link(hospital.id)
        await _move_record_to(db_session, record.id, new_owner.phone)
        assert (await new_owner.link(hospital.id)).status_code == 201

        again = await old_owner.link(hospital.id, mrn=record.mrn)

        assert _error(again) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        [link] = await _active_links(db_session, patient_id=record.id)
        assert link.account_id == new_owner.account_id


class TestPhoneBinding:
    async def test_once_the_hospital_changes_the_phone_the_link_gives_nothing(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
    ) -> None:
        """A recycled or corrected number: the link is reported suspended and serves nothing."""
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        assert (await patient.link(hospital.id)).status_code == 201
        await _share(db_session, patient, hospital.id, other_hospital.id)
        authorization, grants = _standing(db_session)
        reader = Grantee(other_hospital.id)
        read = (record.id, hospital.id, RecordCategory.PRESCRIPTIONS, DOB)
        assert [link["suspended"] for link in await patient.links()] == [False]
        await grants.authorize(reader, *read)

        await _move_record_to(db_session, record.id, new_phone())

        assert [link["suspended"] for link in await patient.links()] == [True]
        account = await _account(db_session, patient.account_id)
        with pytest.raises(NotFoundError):
            await authorization.resolve_context(account, hospital.id)
        with pytest.raises(NotFoundError):
            await grants.create(
                account,
                hospital.id,
                grantee_type=GranteeType.HOSPITAL,
                grantee_id=other_hospital.id,
                grantee_hospital_id=other_hospital.id,
                categories=[RecordCategory.IDENTITY],
                purpose_note=GrantPurposeNote.CONSULTATION,
            )
        with pytest.raises(NotFoundError):
            await grants.authorize(reader, *read)
        assert await _count(db_session, PatientAccessGrant) == 1
        # Evaluated at read time: nothing was written to the link or the grant.
        [link] = await _active_links(db_session, account_id=patient.account_id)
        assert link.unlink_reason is None

    async def test_a_suspended_link_cannot_be_talked_back_into_life(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """Attack: the number's previous holder re-links with the right date of birth and MRN."""
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)
        await _move_record_to(db_session, record.id, new_phone())

        response = await patient.link(hospital.id, mrn=record.mrn)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert [link["suspended"] for link in await patient.links()] == [True]

    async def test_a_deactivated_record_suspends_its_link_and_cannot_be_registered_around(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)
        await db_session.execute(
            update(Patient).where(Patient.id == record.id).values(deleted_at=datetime.now(UTC))
        )
        await db_session.commit()

        assert [link["suspended"] for link in await patient.links()] == [True]
        assert _error(await patient.link(hospital.id))[:2] == (403, "LINK_UNAVAILABLE")
        assert _error(await patient.register(hospital.id))[:2] == (403, "LINK_UNAVAILABLE")
        assert len(await _records_on(db_session, patient.phone)) == 1


# ── 7. Self-registration ─────────────────────────────────────────────────────


class TestSelfRegistration:
    async def test_the_match_is_run_again_by_the_server(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """The client was told "no record" a moment ago; that is not what decides."""
        assert (await patient.link(hospital.id)).status_code == 404
        existing = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.register(hospital.id)

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert [record.id for record in await _records_on(db_session, patient.phone)] == [
            existing.id
        ]
        assert await _active_links(db_session) == []
        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_an_ambiguous_match_blocks_registration_too(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        for _ in range(2):
            await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.register(hospital.id)

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert len(await _records_on(db_session, patient.phone)) == 2

    async def test_the_record_carries_the_verified_phone_and_nothing_staff_only(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        response = await patient.register(hospital.id)

        assert response.status_code == 201, response.text
        [record] = await _records_on(db_session, patient.phone)
        assert record.phone == patient.phone
        assert record.hospital_id == hospital.id
        assert record.created_by is None
        assert (record.first_name, record.last_name, record.date_of_birth, record.gender) == (
            "Asha",
            "Verma",
            DOB,
            Gender.FEMALE,
        )
        [link] = await _active_links(db_session, account_id=patient.account_id)
        assert (link.patient_id, link.verified_via) == (record.id, "self_registration")
        # The response is the patient's own new record, and no identifier beyond its MRN.
        assert str(record.id) not in response.text
        assert patient.phone not in response.text

    @pytest.mark.parametrize(
        "change", ["nothing", "consent_withdrawn", "phone_moved", "link_ended"]
    )
    async def test_an_account_registers_once_per_hospital_whatever_happens_next(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, change: str
    ) -> None:
        """Attack: mint patient records in a hospital's register from one phone."""
        assert (await patient.register(hospital.id)).status_code == 201
        [record] = await _records_on(db_session, patient.phone)
        if change == "consent_withdrawn":
            authorization, _grants = _standing(db_session)
            consent = authorization._consent  # noqa: SLF001 — the service the providers build
            for purpose in (
                ConsentPurpose.HOSPITAL_REGISTRATION,
                ConsentPurpose.HOSPITAL_RECORD_LINK,
            ):
                await consent.withdraw(patient.account_id, purpose, hospital_id=hospital.id)
        elif change == "phone_moved":
            await _move_record_to(db_session, record.id, new_phone())
        elif change == "link_ended":
            await db_session.execute(
                update(PatientAccountLink).values(unlinked_at=datetime.now(UTC))
            )
            await db_session.commit()
        patients_before = await _count(db_session, Patient, Patient.hospital_id == hospital.id)

        for birth in (DOB, OTHER_DOB, date(2001, 2, 3)):
            again = await patient.register(hospital.id, date_of_birth=birth.isoformat())
            assert again.status_code in (403, 409), again.text

        assert (
            await _count(db_session, Patient, Patient.hospital_id == hospital.id) == patients_before
        )

    @pytest.mark.parametrize("version", ["2025-01", "2026-10-DRAFT", "2026-10-draft2", "latest"])
    async def test_it_needs_the_exact_current_consent_version(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, version: str
    ) -> None:
        response = await patient.register(hospital.id, consent_policy_version=version)

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert await _records_on(db_session, patient.phone) == []
        assert await _count(db_session, PatientConsentRecord) == 0
        assert await _active_links(db_session) == []

    async def test_it_records_both_consents_at_the_version_that_was_shown(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        await patient.register(hospital.id)

        consents = (await db_session.execute(select(PatientConsentRecord))).scalars().all()
        assert sorted(
            (c.purpose, c.policy_version, c.hospital_id, c.account_id, c.withdrawn_at)
            for c in consents
        ) == [
            ("hospital_record_link", POLICY, hospital.id, patient.account_id, None),
            ("hospital_registration", POLICY, hospital.id, patient.account_id, None),
        ]

    async def test_a_deactivated_match_is_not_silently_duplicated(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone, deleted=True)

        response = await patient.register(hospital.id)

        assert _error(response) == (403, "LINK_UNAVAILABLE", CONTACT_HOSPITAL)
        assert len(await _records_on(db_session, patient.phone)) == 1
        assert await _count(db_session, PatientConsentRecord) == 0


# ── 8. The same, at the same moment ──────────────────────────────────────────


class _RealWorld:
    """Independent connections to the test database. Everything is committed."""

    def __init__(self, engine: AsyncEngine) -> None:
        self.factory = async_sessionmaker(engine, expire_on_commit=False)
        self.application = create_app()
        # The application's own request-scoped dependency then opens one real
        # session (one pooled connection) per request, as it does in service.
        self.application.state.db_session_factory = self.factory
        self.application.dependency_overrides[get_sms_sender] = lambda: None
        self.hospital_ids: list[uuid.UUID] = []
        self.account_ids: list[uuid.UUID] = []
        self.clients: list[AsyncClient] = []

    async def hospital(self) -> Hospital:
        async with self.factory() as session:
            hospital = Hospital(
                id=uuid.uuid4(),
                name="Linking Concurrency Hospital",
                slug=f"linking-{uuid.uuid4().hex[:12]}",
                address={"line1": "1 Test Road", "city": "Hyderabad", "country": "IN"},
                settings={PATIENT_APP_ENABLED: True},
            )
            session.add(hospital)
            await session.commit()
        self.hospital_ids.append(hospital.id)
        return hospital

    async def caller(self) -> _Caller:
        client = patient_client(self.application)
        self.clients.append(client)
        async with self.factory() as session:
            caller = await _enrol(session, client)
        self.account_ids.append(caller.account_id)
        return caller

    async def record(self, hospital_id: uuid.UUID, phone: str, **values: Any) -> Patient:
        async with self.factory() as session:
            return await insert_patient_record(session, hospital_id, phone=phone, **values)

    async def move_record_to(self, record_id: uuid.UUID, phone: str) -> None:
        async with self.factory() as session:
            await _move_record_to(session, record_id, phone)

    async def active_links(self, **criteria: Any) -> list[PatientAccountLink]:
        async with self.factory() as session:
            return await _active_links(session, **criteria)

    async def count(self, model: Any, *criteria: Any) -> int:
        async with self.factory() as session:
            return await _count(session, model, *criteria)

    async def cleanup(self) -> None:
        """Delete every row this test committed: by account and by hospital."""
        for client in self.clients:
            await client.aclose()
        self.application.dependency_overrides.clear()
        accounts, hospitals = self.account_ids, self.hospital_ids
        keys = [
            bucket(kind, account_id, hospital_id).key_hash
            for kind in (BucketKind.PT_LINK_ATTEMPT, BucketKind.PT_LINK_DAILY)
            for account_id in accounts
            for hospital_id in hospitals
        ]
        keys += [
            bucket(BucketKind.PT_LINK_ATTEMPT, account_id, "unavailable").key_hash
            for account_id in accounts
        ]
        async with self.factory() as session:
            await session.execute(
                delete(AuthThrottleBucket).where(AuthThrottleBucket.key_hash.in_(keys))
            )
            await session.execute(
                delete(AuditLog).where(
                    or_(
                        AuditLog.hospital_id.in_(hospitals),
                        AuditLog.patient_account_id.in_(accounts),
                    )
                )
            )
            await session.execute(
                delete(PatientAccessGrant).where(PatientAccessGrant.hospital_id.in_(hospitals))
            )
            await session.execute(
                delete(PatientConsentRecord).where(PatientConsentRecord.account_id.in_(accounts))
            )
            await session.execute(
                delete(PatientAccountLink).where(PatientAccountLink.hospital_id.in_(hospitals))
            )
            await session.execute(delete(Patient).where(Patient.hospital_id.in_(hospitals)))
            await session.execute(delete(MrnSequence).where(MrnSequence.hospital_id.in_(hospitals)))
            await session.execute(delete(PatientAccount).where(PatientAccount.id.in_(accounts)))
            await session.execute(delete(Hospital).where(Hospital.id.in_(hospitals)))
            await session.commit()


@pytest_asyncio.fixture
async def real(db_engine: AsyncEngine) -> AsyncGenerator[_RealWorld]:
    """Independent connections with real commits; cleaned up whatever happens."""
    engine = create_async_engine(db_engine.url, pool_size=8, max_overflow=0, pool_timeout=120)
    world = _RealWorld(engine)
    try:
        yield world
    finally:
        try:
            await world.cleanup()
        finally:
            await engine.dispose()


@dataclass
class _Race:
    """An in-flight link that matched a record just before the hospital moved its phone."""

    record: Patient
    early: _Caller
    early_response: Response
    rightful: _Caller
    rightful_response: Response


class TestConcurrentLinking:
    """Requests that arrive at the same moment, each on its own connection."""

    async def test_one_account_linking_in_parallel_ends_with_one_link(
        self, real: _RealWorld
    ) -> None:
        """Attack: fire the same link several times so that each sees "not linked yet"."""
        hospital = await real.hospital()
        caller = await real.caller()
        record = await real.record(hospital.id, caller.phone)

        responses = await asyncio.gather(*(caller.link(hospital.id) for _ in range(4)))

        assert sorted(r.status_code for r in responses) == [200, 200, 200, 201]
        [link] = await real.active_links(account_id=caller.account_id)
        assert link.patient_id == record.id
        assert (
            await real.count(PatientAccountLink, PatientAccountLink.account_id == caller.account_id)
            == 1
        )
        assert (
            await real.count(
                PatientConsentRecord, PatientConsentRecord.account_id == caller.account_id
            )
            == 1
        )
        assert (
            await real.count(
                AuditLog,
                AuditLog.patient_account_id == caller.account_id,
                AuditLog.action == "patient.link.created",
            )
            == 1
        )

    async def test_one_account_cannot_get_two_self_links_at_one_hospital(
        self, real: _RealWorld
    ) -> None:
        """Attack: link two different records of one hospital at the same moment."""
        hospital = await real.hospital()
        caller = await real.caller()
        await real.record(hospital.id, caller.phone)
        await real.record(hospital.id, caller.phone, date_of_birth=OTHER_DOB)

        responses = await asyncio.gather(
            caller.link(hospital.id),
            caller.link(hospital.id, date_of_birth=OTHER_DOB.isoformat()),
            caller.link(hospital.id),
            caller.link(hospital.id, date_of_birth=OTHER_DOB.isoformat()),
        )

        statuses = sorted(r.status_code for r in responses)
        assert statuses == [200, 201, 409, 409]
        assert len(await real.active_links(account_id=caller.account_id)) == 1
        assert (
            await real.count(PatientAccountLink, PatientAccountLink.account_id == caller.account_id)
            == 1
        )

    async def test_the_database_refuses_a_second_active_link_to_one_record(
        self, real: _RealWorld
    ) -> None:
        """Whatever a service believed when it looked: two inserts, two connections, one row."""
        hospital = await real.hospital()
        record = await real.record(hospital.id, new_phone())
        claimants = [await real.caller() for _ in range(4)]
        ready = asyncio.Barrier(len(claimants))

        async def _claim(caller: _Caller) -> bool:
            async with real.factory() as session:
                links = PatientAccountLinkRepository(session)
                assert await links.get_active_for_patient(hospital.id, record.id) is None
                await ready.wait()  # every claimant has now seen "nobody holds it"
                try:
                    await links.create_link(
                        hospital.id,
                        account_id=caller.account_id,
                        patient_id=record.id,
                        verified_via="phone_dob",
                        now=datetime.now(UTC),
                    )
                    await session.commit()
                except IntegrityError:
                    await session.rollback()
                    return False
                return True

        won = await asyncio.gather(*(_claim(caller) for caller in claimants))

        assert sorted(won) == [False, False, False, True]
        assert len(await real.active_links(patient_id=record.id)) == 1

    async def _race(self, real: _RealWorld, monkeypatch: pytest.MonkeyPatch) -> _Race:
        """One account matches a record; before it links, the hospital moves the phone.

        The first request is held between its match and its insert — the
        window in which what it read stops being true — while the hospital
        corrects the number and the number's owner links. Nothing of the
        service is replaced: only when its match returns.
        """
        hospital = await real.hospital()
        early, rightful = await real.caller(), await real.caller()
        record = await real.record(hospital.id, early.phone)
        matched, release = asyncio.Event(), asyncio.Event()
        real_match = RecordLinkService._match  # noqa: SLF001 — held, not replaced

        async def _match(
            service: RecordLinkService, hospital_id: uuid.UUID, phone: str, date_of_birth: date
        ) -> Any:
            result = await real_match(service, hospital_id, phone, date_of_birth)
            if phone == early.phone:
                matched.set()
                await asyncio.wait_for(release.wait(), timeout=60)
            return result

        monkeypatch.setattr(RecordLinkService, "_match", _match)
        in_flight = asyncio.create_task(early.link(hospital.id))
        try:
            await asyncio.wait_for(matched.wait(), timeout=60)
            await real.move_record_to(record.id, rightful.phone)
            rightful_response = await rightful.link(hospital.id)
        finally:
            release.set()
        early_response = await in_flight
        return _Race(record, early, early_response, rightful, rightful_response)

    async def test_two_accounts_never_both_hold_one_record(
        self, real: _RealWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: link in the instant the hospital moves the record to somebody else's number.

        However the two requests interleave, one account at most holds the
        record, and the one whose phone is no longer on it is served nothing.
        """
        race = await self._race(real, monkeypatch)

        assert race.rightful_response.status_code == 201, race.rightful_response.text
        assert len(await real.active_links(patient_id=race.record.id)) <= 1
        assert [link["suspended"] for link in await race.early.links()] in ([], [True])
        async with real.factory() as session:
            authorization, _grants = _standing(session)
            with pytest.raises(NotFoundError):
                await authorization.resolve_context(
                    await _account(session, race.early.account_id), race.record.hospital_id
                )
        # Whatever happened, the number's owner can (re-)establish the link.
        assert (await race.rightful.link(race.record.hospital_id)).status_code in (200, 201)
        [link] = await real.active_links(patient_id=race.record.id)
        assert link.account_id == race.rightful.account_id

    async def test_an_in_flight_request_does_not_end_the_rightful_owners_fresh_link(
        self, real: _RealWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        race = await self._race(real, monkeypatch)

        assert race.rightful_response.status_code == 201
        [link] = await real.active_links(patient_id=race.record.id)
        assert link.account_id == race.rightful.account_id

    async def test_simultaneous_registrations_create_one_record(self, real: _RealWorld) -> None:
        """Attack: a double submit — or a script — registering the same person many times."""
        hospital = await real.hospital()
        caller = await real.caller()

        responses = await asyncio.gather(*(caller.register(hospital.id) for _ in range(4)))

        assert sorted(r.status_code for r in responses) == [201, 409, 409, 409]
        assert (
            await real.count(
                Patient, Patient.hospital_id == hospital.id, Patient.phone == caller.phone
            )
            == 1
        )
        [link] = await real.active_links(account_id=caller.account_id)
        async with real.factory() as session:
            [record] = await _records_on(session, caller.phone)
        assert link.patient_id == record.id
        assert (
            await real.count(
                PatientConsentRecord,
                PatientConsentRecord.account_id == caller.account_id,
                PatientConsentRecord.purpose == "hospital_registration",
            )
            == 1
        )

    async def test_registering_and_linking_at_once_never_duplicates_the_record(
        self, real: _RealWorld
    ) -> None:
        """A record that exists is linked, never registered again beside itself."""
        hospital = await real.hospital()
        caller = await real.caller()
        record = await real.record(hospital.id, caller.phone)

        responses = await asyncio.gather(
            caller.register(hospital.id),
            caller.link(hospital.id),
            caller.register(hospital.id),
        )

        assert sorted(r.status_code for r in responses) == [201, 409, 409]
        assert await real.count(Patient, Patient.hospital_id == hospital.id) == 1
        [link] = await real.active_links(account_id=caller.account_id)
        assert link.patient_id == record.id


# ── 9. The hospital is not an oracle, and probing it is bounded ──────────────

#: Documented number (PT_LINK_DAILY: burst 10, one back every 144 minutes),
#: restated on purpose.
DAILY_LINK_ATTEMPTS = 10
UNAVAILABLE_ATTEMPTS = 5


async def _unusable(session: AsyncSession, other_hospital_id: uuid.UUID, state: str) -> object:
    """A hospital reference that cannot be used, in one of the ways it can be unusable."""
    if state == "flag_off":
        await open_hospital(session, other_hospital_id, enabled=False)
        return other_hospital_id
    if state == "inactive":
        await open_hospital(session, other_hospital_id)
        await session.execute(
            update(Hospital).where(Hospital.id == other_hospital_id).values(is_active=False)
        )
        await session.commit()
        return other_hospital_id
    if state == "unknown":
        return uuid.uuid4()
    return "no such hospital!"


class TestTheHospitalIsNotAnOracle:
    """What is refused whatever the hospital must not say anything about the hospital."""

    @pytest.mark.parametrize("state", ["flag_off", "inactive", "unknown", "garbage"])
    @pytest.mark.parametrize("operation", ["link", "register"])
    async def test_a_stale_consent_version_says_nothing_about_the_hospital(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
        patient: _Caller,
        state: str,
        operation: str,
    ) -> None:
        """Attack: send a wrong consent version to learn which hospital references are live.

        The version check needs no date of birth and costs no attempt. If an
        open hospital answered it with ``409`` and every other reference with
        ``404``, it would be a free, unthrottled list of the hospitals that
        use the Patient App.
        """
        reference = await _unusable(db_session, other_hospital_id, state)
        ask = patient.link if operation == "link" else patient.register

        at_an_open_hospital = await ask(hospital.id, consent_policy_version="2025-01")
        at_an_unusable_one = await ask(reference, consent_policy_version="2025-01")

        assert at_an_open_hospital.status_code == 409
        assert _wire(at_an_unusable_one) == _wire(at_an_open_hospital)
        # And it cost, wrote and recorded nothing — at either.
        assert await _attempts(db_session, patient.account_id) == []
        assert await _count(db_session, AuthThrottleBucket) == 0
        assert await _count(db_session, PatientConsentRecord) == 0

    @pytest.mark.parametrize("state", ["flag_off", "inactive", "unknown", "garbage"])
    @pytest.mark.parametrize("version", [POLICY, "2025-01"])
    async def test_the_same_request_gets_the_same_answer_at_every_unusable_hospital(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
        patient: _Caller,
        state: str,
        version: str,
    ) -> None:
        """With the current version: the ``404`` of "no record". With a stale one: its ``409``."""
        reference = await _unusable(db_session, other_hospital_id, state)

        here = await patient.link(hospital.id, consent_policy_version=version)
        there = await patient.link(reference, consent_policy_version=version)

        assert here.status_code == (404 if version == POLICY else 409)
        assert _wire(there) == _wire(here)

    @pytest.mark.parametrize("state", ["open", "flag_off", "unknown", "garbage"])
    async def test_an_invalid_registration_is_refused_before_the_hospital_is_looked_at(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital_id: uuid.UUID,
        patient: _Caller,
        state: str,
    ) -> None:
        """Details that fail the patient rules are one answer, whatever the hospital."""
        reference = (
            hospital.id
            if state == "open"
            else await _unusable(db_session, other_hospital_id, state)
        )
        linking = _linking(db_session)
        account = await _account(db_session, patient.account_id)
        # Built without the request schema, so only the service stands in the way.
        payload = RegisterRecordRequest.model_construct(
            first_name="",
            last_name="Verma",
            date_of_birth=DOB,
            gender=Gender.FEMALE,
            consent_policy_version=POLICY,
        )

        with pytest.raises(ValidationError):
            await linking.register(account, str(reference), payload, client=ClientContext())

        assert await _attempts(db_session, patient.account_id) == []
        assert await _count(db_session, AuthThrottleBucket) == 0
        assert await _records_on(db_session, patient.phone) == []

    async def test_attempts_at_unusable_hospitals_are_audited_only_within_an_allowance(
        self, db_session: AsyncSession, patient: _Caller, clock: ThrottleClock
    ) -> None:
        """Attack: loop over invented hospital references to flood the audit trail.

        There is no hospital to count such an attempt against, so it is
        charged to the account alone — before anything is written. Once the
        allowance is gone the answer is the same ``404`` and nothing is added.
        """
        answers = [await patient.link(uuid.uuid4()) for _ in range(UNAVAILABLE_ATTEMPTS + 20)]

        assert len({_frozen(answer) for answer in answers}) == 1
        assert _error(answers[0]) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _outcomes(db_session, patient.account_id) == Counter(
            {"hospital_unavailable": UNAVAILABLE_ATTEMPTS}
        )
        # Registration draws on the same allowance: it is not a second tap.
        assert (await patient.register("no such hospital!")).status_code == 404
        assert len(await _attempts(db_session, patient.account_id)) == UNAVAILABLE_ATTEMPTS
        # One comes back every twelve minutes — one, not five.
        clock.advance(minutes=12, seconds=1)
        for _ in range(3):
            assert (await patient.link(uuid.uuid4())).status_code == 404
        assert len(await _attempts(db_session, patient.account_id)) == UNAVAILABLE_ATTEMPTS + 1

    async def test_the_charge_comes_before_the_audit_row(
        self, db_session: AsyncSession, patient: _Caller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Charge first: an attempt that could not be charged is never written down."""
        order: list[str] = []
        real_admit, real_record = AuthThrottle.admit, AuditService.record

        async def _admit(throttle: AuthThrottle, buckets: Any) -> Any:
            order.append("charged")
            return await real_admit(throttle, buckets)

        async def _record(service: AuditService, event: Any) -> None:
            order.append("audited")
            await real_record(service, event)

        monkeypatch.setattr(AuthThrottle, "admit", _admit)
        monkeypatch.setattr(AuditService, "record", _record)

        assert (await patient.link(uuid.uuid4())).status_code == 404

        assert order == ["charged", "audited"]
        [row] = (await db_session.execute(select(AuthThrottleBucket))).scalars().all()
        assert row.kind == "pt_link_attempt"
        assert row.key_hash == (
            bucket(BucketKind.PT_LINK_ATTEMPT, patient.account_id, "unavailable").key_hash
        )

    async def test_that_allowance_is_one_accounts_and_touches_no_real_hospital(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """Attack: use up "unusable hospital" attempts to stop somebody — or yourself — linking."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        for _ in range(UNAVAILABLE_ATTEMPTS + 5):
            await patient.link(uuid.uuid4())

        # Another account's attempts are still recorded …
        assert (await stranger.link(uuid.uuid4())).status_code == 404
        assert await _outcomes(db_session, stranger.account_id) == Counter(
            {"hospital_unavailable": 1}
        )
        # … and the account that used it up can still link where it has a record.
        assert (await patient.link(hospital.id)).status_code == 201


def _frozen(response: Response) -> Any:
    """A response's visible answer, in a form that can go in a set."""
    return json.dumps(_wire(response), sort_keys=True, default=str)


def _linking(session: AsyncSession) -> RecordLinkService:
    """The link service, composed as ``app/api/dependencies/patient.py`` composes it."""
    uow = UnitOfWork(session)
    audit = AuditService(session, AuditLogRepository(session), UserRepository(session))
    links = PatientAccountLinkRepository(session)
    patient_records = PatientRepository(session)
    return RecordLinkService(
        PatientHospitalGate(HospitalRepository(session)),
        links,
        patient_records,
        PatientService(patient_records, MRNService(MrnSequenceRepository(session)), session, audit),
        ConsentService(PatientConsentRepository(session), links, uow, audit),
        throttle=AuthThrottle(AuthThrottleRepository(session), uow),
        uow=uow,
        audit=audit,
    )


class TestDailyAttemptCap:
    """§4.5: five attempts an hour **and** ten a day, per account and hospital."""

    async def _guess(self, caller: _Caller, hospital: Hospital, number: int) -> int:
        guess = (OTHER_DOB + timedelta(days=number)).isoformat()
        return (await caller.link(hospital.id, date_of_birth=guess)).status_code

    async def test_ten_guesses_in_a_day_and_then_none_whatever_the_hourly_allowance_says(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, clock: ThrottleClock
    ) -> None:
        """Attack: come back every hour and work through dates of birth five at a time.

        With the hourly allowance alone that is 125 guesses a day. The daily
        allowance stops it at ten, and then lets one through every 144 minutes.
        """
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        first_hour = [await self._guess(patient, hospital, n) for n in range(6)]
        clock.advance(minutes=60, seconds=1)  # the hourly allowance is whole again
        second_hour = [await self._guess(patient, hospital, 10 + n) for n in range(6)]
        clock.advance(minutes=60, seconds=1)  # and again — but the day's is spent
        third_hour = [await self._guess(patient, hospital, 20 + n) for n in range(3)]
        right_details = await patient.link(hospital.id)

        assert first_hour == [404] * LINK_ATTEMPTS + [403]
        assert second_hour == [404] * LINK_ATTEMPTS + [403]
        assert third_hour == [403] * 3
        assert _error(right_details) == (403, "LINK_UNAVAILABLE", CONTACT_HOSPITAL)
        assert (await _outcomes(db_session, patient.account_id))["no_match"] == DAILY_LINK_ATTEMPTS
        assert await _active_links(db_session) == []
        # 144 minutes after the first guess, one comes back — one, not ten.
        clock.advance(minutes=24, seconds=1)
        assert await self._guess(patient, hospital, 30) == 404
        assert await self._guess(patient, hospital, 31) == 403

    async def test_the_daily_refusal_is_the_hourly_refusal(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, clock: ThrottleClock
    ) -> None:
        """Which of the two allowances ran out is not something the answer says."""
        hourly = [await patient.link(hospital.id) for _ in range(LINK_ATTEMPTS + 1)][-1]
        clock.advance(minutes=60, seconds=1)
        for _ in range(LINK_ATTEMPTS):
            await patient.link(hospital.id)
        clock.advance(minutes=60, seconds=1)
        daily = await patient.link(hospital.id)

        assert hourly.status_code == daily.status_code == 403
        assert _wire(hourly) == _wire(daily)

    async def test_it_is_per_account_and_per_hospital(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        other_hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        clock: ThrottleClock,
    ) -> None:
        """Attack: spend your own day's attempts to shut somebody else — or another hospital — out."""
        await insert_patient_record(db_session, hospital.id, phone=stranger.phone)
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        for _ in range(2):
            for _ in range(LINK_ATTEMPTS):
                await patient.link(hospital.id)
            clock.advance(minutes=60, seconds=1)
        assert (await patient.link(hospital.id)).status_code == 403

        assert (await stranger.link(hospital.id)).status_code == 201
        assert (await patient.link(other_hospital.id)).status_code == 201

    async def test_an_attempt_that_ends_in_a_link_is_given_back_on_both_allowances(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        """The owner confirming their own link is never what runs the day's allowance out."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        statuses = [
            (await patient.link(hospital.id)).status_code for _ in range(DAILY_LINK_ATTEMPTS + 3)
        ]

        assert statuses == [201] + [200] * (DAILY_LINK_ATTEMPTS + 2)

    async def test_both_allowances_are_charged_to_the_account_and_hospital(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller
    ) -> None:
        await patient.link(hospital.id)

        rows = (await db_session.execute(select(AuthThrottleBucket))).scalars().all()
        assert {(row.kind, row.key_hash) for row in rows} == {
            (kind.value, bucket(kind, patient.account_id, hospital.id).key_hash)
            for kind in (BucketKind.PT_LINK_ATTEMPT, BucketKind.PT_LINK_DAILY)
        }


# ── 10. A record that moved, and consents accepted at the same moment ────────


class TestALinkNeverEndsAnHonouredLink:
    """Superseding another account's link is allowed only when that link is truly stale."""

    async def test_a_request_that_matched_before_the_record_moved_is_a_plain_no_match(
        self, real: _RealWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Attack: link in the instant the hospital moves the record to its real owner's number.

        The early request's answer is exactly the one for "no record matches",
        and it is audited as such. The rightful owner's link is not ended, no
        ``patient.link.ended`` is written, and nothing the early request began
        is left behind.
        """
        race = await TestConcurrentLinking()._race(real, monkeypatch)  # noqa: SLF001

        assert race.rightful_response.status_code == 201
        assert _error(race.early_response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        [link] = await real.active_links(patient_id=race.record.id)
        assert link.account_id == race.rightful.account_id
        assert await real.active_links(account_id=race.early.account_id) == []
        assert (
            await real.count(
                AuditLog,
                AuditLog.action == "patient.link.ended",
                AuditLog.hospital_id == race.record.hospital_id,
            )
            == 0
        )
        async with real.factory() as session:
            assert await _outcomes(session, race.early.account_id) == Counter({"no_match": 1})
            assert (
                await _count(
                    session,
                    PatientConsentRecord,
                    PatientConsentRecord.account_id == race.early.account_id,
                )
                == 0
            )

    async def test_a_genuinely_stale_link_still_gives_way(
        self, db_session: AsyncSession, hospital: Hospital, patient: _Caller, stranger: _Caller
    ) -> None:
        """The rule the lock protects, unchanged: a recycled number's new owner takes the link."""
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        assert (await patient.link(hospital.id)).status_code == 201
        await _move_record_to(db_session, record.id, stranger.phone)

        assert (await stranger.link(hospital.id)).status_code == 201

        [link] = await _active_links(db_session, patient_id=record.id)
        assert link.account_id == stranger.account_id

    @pytest.mark.parametrize("moved", ["to_a_third_number", "deactivated"])
    async def test_the_record_is_read_again_under_lock_before_another_link_is_ended(
        self,
        db_session: AsyncSession,
        hospital: Hospital,
        patient: _Caller,
        stranger: _Caller,
        monkeypatch: pytest.MonkeyPatch,
        moved: str,
    ) -> None:
        """What the request matched is not trusted at the moment it would end a link.

        The record changes between the match and the link — the window is
        opened by holding the match's result. The other account's link, which
        the earlier read made look stale, is left alone.
        """
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        # By value: the refused request rolls the shared session back, which
        # expires every loaded row.
        record_id = record.id
        assert (await patient.link(hospital.id)).status_code == 201
        await _move_record_to(db_session, record_id, stranger.phone)
        real_match = RecordLinkService._match  # noqa: SLF001 — held, not replaced
        locked: list[str] = []
        real_execute = AsyncSession.execute

        async def _match(service: RecordLinkService, *args: Any) -> Any:
            result = await real_match(service, *args)
            if moved == "to_a_third_number":
                await _move_record_to(db_session, record_id, new_phone())
            else:
                await db_session.execute(
                    update(Patient)
                    .where(Patient.id == record_id)
                    .values(deleted_at=datetime.now(UTC))
                )
            return result

        async def _execute(session: AsyncSession, statement: Any, *args: Any, **kw: Any) -> Any:
            sql = str(statement)
            if "FOR UPDATE" in sql and "FROM patients" in sql:
                locked.append(sql)
            return await real_execute(session, statement, *args, **kw)

        monkeypatch.setattr(RecordLinkService, "_match", _match)
        monkeypatch.setattr(AsyncSession, "execute", _execute)

        response = await stranger.link(hospital.id)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert len(locked) == 1, "the record was not re-read under a row lock"
        [link] = await _active_links(db_session, patient_id=record_id)
        assert link.account_id == patient.account_id
        assert await audit_rows(db_session, "patient.link.ended") == []
        assert (await _outcomes(db_session, stranger.account_id))["no_match"] == 1


class TestConsentsAcceptedAtTheSameMoment:
    """Two acceptances of one policy at once: one consent, and neither request fails."""

    async def test_a_duplicate_that_loses_the_race_answers_with_the_consent_in_force(
        self, db_session: AsyncSession, patient: _Caller, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The race, made exact: the second acceptance does not see the first.

        Its insert is refused by the unique index. That must roll back the
        insert alone — to a savepoint — and answer with the consent that is in
        force, not fail the request and not disturb what the transaction
        already holds.
        """
        consent = _linking(db_session)._consent  # noqa: SLF001 — the service the providers build
        first = await consent.accept(
            patient.account_id, ConsentPurpose.PRIVACY_NOTICE, policy_version=POLICY
        )
        real_get_active = PatientConsentRepository.get_active
        blind = [True]

        async def _get_active(repository: PatientConsentRepository, *args: Any, **kw: Any) -> Any:
            if blind[0]:
                blind[0] = False
                return None  # what a request racing the first one would have read
            return await real_get_active(repository, *args, **kw)

        monkeypatch.setattr(PatientConsentRepository, "get_active", _get_active)
        # Something this request's transaction already holds, to prove it survives.
        marker = await _enrol(db_session, patient.client)

        second = await consent.accept(
            patient.account_id, ConsentPurpose.PRIVACY_NOTICE, policy_version=POLICY
        )

        assert second.id == first.id
        assert await _count(db_session, PatientConsentRecord) == 1
        assert len(await audit_rows(db_session, "patient.consent.granted")) == 1
        assert await _count(db_session, PatientAccount, PatientAccount.id == marker.account_id) == 1

    async def test_an_integrity_error_that_is_not_a_duplicate_is_not_swallowed(
        self, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail closed: never report a consent that is not on record."""
        consent = _linking(db_session)._consent  # noqa: SLF001

        with pytest.raises(IntegrityError):  # no such account: a foreign key, not a duplicate
            await consent.accept(uuid.uuid4(), ConsentPurpose.PRIVACY_NOTICE, policy_version=POLICY)
        await db_session.rollback()

        assert await _count(db_session, PatientConsentRecord) == 0

    async def test_many_simultaneous_acceptances_leave_one_consent_and_no_error(
        self, real: _RealWorld, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On real connections: every request has read "not accepted yet" before any inserts."""
        caller = await real.caller()
        racers = 6
        everyone_has_looked = asyncio.Barrier(racers)
        real_add = PatientConsentRepository.add

        async def _add(repository: PatientConsentRepository, *args: Any, **kw: Any) -> Any:
            await asyncio.wait_for(everyone_has_looked.wait(), timeout=60)
            return await real_add(repository, *args, **kw)

        monkeypatch.setattr(PatientConsentRepository, "add", _add)

        async def _accept() -> uuid.UUID:
            async with real.factory() as session:
                record = await _linking(session)._consent.accept(  # noqa: SLF001
                    caller.account_id, ConsentPurpose.TERMS_OF_SERVICE, policy_version=POLICY
                )
                return record.id

        accepted = await asyncio.gather(*(_accept() for _ in range(racers)))

        assert len(set(accepted)) == 1
        assert (
            await real.count(
                PatientConsentRecord, PatientConsentRecord.account_id == caller.account_id
            )
            == 1
        )
        assert (
            await real.count(
                AuditLog,
                AuditLog.action == "patient.consent.granted",
                AuditLog.patient_account_id == caller.account_id,
            )
            == 1
        )
