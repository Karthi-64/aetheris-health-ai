"""API tests for Patient App record links — link, self-register, and the links in ``/me``.

``docs/modules/15-patient-app.md`` §4.4, §4.5 and §27.4: every row of the
outcome table, over HTTP, against a real PostgreSQL. The patient signs in the
way a browser does; hospital-side rows (hospitals, patient records) are
inserted directly, as staff would have created them.
"""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from app.core.config import settings
from app.core.security import create_access_token
from app.models.hospital import Hospital
from app.models.patient import Patient
from app.models.patient_account import PatientAccountLink
from app.models.patient_consent import PatientConsentRecord
from app.tests.patient_app_helpers import (
    PATIENT,
    POLICY,
    FakeSmsSender,
    ThrottleClock,
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
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from httpx import AsyncClient, Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

ME = f"{PATIENT}/me"
DOB = date(1990, 5, 17)
OTHER_DOB = date(1985, 1, 2)
NO_MATCH = "We could not find a record with these details."

# Documented number, restated on purpose.
LINK_ATTEMPTS = 5


@pytest.fixture(autouse=True)
def _patient_test_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "PATIENT_COOKIE_SECURE", False)
    monkeypatch.setattr(settings, "RATE_LIMIT_ANON_PER_MIN", 1_000_000)
    monkeypatch.setattr(settings, "RATE_LIMIT_USER_PER_MIN", 1_000_000)


@pytest.fixture
def sms() -> FakeSmsSender:
    return FakeSmsSender()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> ThrottleClock:
    return ThrottleClock(monkeypatch)


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, sms: FakeSmsSender) -> AsyncGenerator[FastAPI]:
    app = build_patient_application(db_session, sms)
    yield app
    app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def browser(application: FastAPI) -> AsyncGenerator[AsyncClient]:
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


class _Patient:
    """A signed-in patient: a phone, a browser and an access token."""

    def __init__(self, client: AsyncClient, phone: str, session: dict[str, Any]) -> None:
        self.client = client
        self.phone = phone
        self.account_id = uuid.UUID(session["account"]["id"])
        self.headers = bearer(session["access_token"])

    async def link(self, hospital_ref: object, **body: Any) -> Response:
        payload = {"date_of_birth": DOB.isoformat(), "consent_policy_version": POLICY, **body}
        return await self.client.post(
            f"{PATIENT}/hospitals/{hospital_ref}/link", json=payload, headers=self.headers
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

    async def me(self) -> dict[str, Any]:
        response = await self.client.get(ME, headers=self.headers)
        assert response.status_code == 200, response.text
        data: dict[str, Any] = response.json()["data"]
        return data


async def _signed_in(client: AsyncClient, sms: FakeSmsSender, phone: str | None = None) -> _Patient:
    phone = phone or new_phone()
    return _Patient(client, phone, await sign_in(client, sms, phone))


@pytest_asyncio.fixture
async def patient(browser: AsyncClient, sms: FakeSmsSender) -> _Patient:
    return await _signed_in(browser, sms)


async def _links(session: AsyncSession, account_id: uuid.UUID) -> list[PatientAccountLink]:
    result = await session.execute(
        select(PatientAccountLink)
        .where(PatientAccountLink.account_id == account_id)
        .order_by(PatientAccountLink.linked_at, PatientAccountLink.id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _consents(session: AsyncSession, account_id: uuid.UUID) -> list[PatientConsentRecord]:
    result = await session.execute(
        select(PatientConsentRecord)
        .where(PatientConsentRecord.account_id == account_id)
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


def _error(response: Response) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body["error_code"], body["message"]


def _without_request_id(response: Response) -> dict[str, Any]:
    body: dict[str, Any] = response.json()
    body.pop("metadata", None)
    return body


async def _attempt_outcomes(session: AsyncSession) -> list[str]:
    rows = await audit_rows(session, "patient.link.attempted")
    return sorted(str(row.context["outcome"]) for row in rows if row.context)


# ── Link: the outcome table of §4.5 ──────────────────────────────────────────


class TestLink:
    async def test_a_unique_match_links_the_account(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id)

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert set(data) == {
            "hospital_id",
            "hospital_ref",
            "hospital_name",
            "linked_at",
            "suspended",
        }
        assert data["hospital_id"] == str(hospital.id)
        assert data["hospital_ref"] == hospital.slug
        assert data["hospital_name"] == hospital.name
        assert data["suspended"] is False
        [link] = await _links(db_session, patient.account_id)
        assert link.patient_id == record.id
        assert link.hospital_id == hospital.id
        assert link.relationship == "self"
        assert link.verified_via == "phone_dob"
        assert link.unlinked_at is None

    async def test_the_response_carries_nothing_from_the_record(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id)

        for secret in (
            str(record.id),
            record.mrn,
            record.first_name,
            record.last_name,
            patient.phone,
        ):
            assert secret not in response.text

    async def test_the_hospital_can_be_named_by_its_code(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.slug)

        assert response.status_code == 201, response.text
        assert response.json()["data"]["hospital_id"] == str(hospital.id)

    async def test_linking_records_the_consent_it_was_given_under(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        await patient.link(hospital.id)

        [consent] = await _consents(db_session, patient.account_id)
        assert consent.purpose == "hospital_record_link"
        assert consent.hospital_id == hospital.id
        assert consent.policy_version == POLICY
        assert consent.withdrawn_at is None
        assert consent.ip_address is not None

    async def test_it_is_audited_without_the_date_of_birth_or_the_mrn(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        await patient.link(hospital.id, mrn=record.mrn)

        [attempt] = await audit_rows(db_session, "patient.link.attempted")
        [created] = await audit_rows(db_session, "patient.link.created")
        [granted] = await audit_rows(db_session, "patient.consent.granted")
        assert attempt.context == {"operation": "link", "outcome": "linked", "mrn_supplied": True}
        for row in (attempt, created, granted):
            assert row.actor_type == "patient"
            assert row.patient_account_id == patient.account_id
            assert row.actor_user_id is None
            assert row.hospital_id == hospital.id
            stored = json.dumps(row.context)
            assert DOB.isoformat() not in stored
            assert record.mrn not in stored
            assert patient.phone not in stored

    async def test_no_record_on_the_phone_is_a_404(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=new_phone())

        response = await patient.link(hospital.id)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _links(db_session, patient.account_id) == []
        assert await _attempt_outcomes(db_session) == ["no_match"]

    async def test_a_wrong_date_of_birth_is_indistinguishable_from_no_record(
        self, application: FastAPI, sms: FakeSmsSender, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        """Attack: learn that a phone is on somebody's record by trying dates of birth."""
        async with patient_client(application) as one, patient_client(application) as two:
            on_a_record = await _signed_in(one, sms)
            on_no_record = await _signed_in(two, sms)
            await insert_patient_record(db_session, hospital.id, phone=on_a_record.phone)

            wrong_dob = await on_a_record.link(hospital.id, date_of_birth=OTHER_DOB.isoformat())
            no_record = await on_no_record.link(hospital.id, date_of_birth=OTHER_DOB.isoformat())

        assert wrong_dob.status_code == no_record.status_code == 404
        assert _without_request_id(wrong_dob) == _without_request_id(no_record)
        assert await _attempt_outcomes(db_session) == ["no_match", "no_match"]

    async def test_the_phone_comes_from_the_account_never_from_the_request(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        """Attack: name somebody else's phone (or record) in the request."""
        victim_phone = new_phone()
        victim = await insert_patient_record(db_session, hospital.id, phone=victim_phone)

        for extra in ({"phone": victim_phone}, {"patient_id": str(victim.id)}):
            response = await patient.link(hospital.id, **extra)
            assert response.status_code == 422, response.text
        honest = await patient.link(hospital.id)

        assert honest.status_code == 404
        assert await _links(db_session, patient.account_id) == []

    async def test_two_matches_need_an_mrn(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        first = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id)

        assert _error(response)[:2] == (409, "LINK_MRN_REQUIRED")
        for secret in (first.mrn, str(first.id), "2", "two"):
            assert secret not in response.json()["message"]
        assert await _links(db_session, patient.account_id) == []
        assert await _attempt_outcomes(db_session) == ["mrn_required"]

    async def test_the_right_mrn_selects_one_of_two_matches(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        wanted = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id, mrn=f"  {wanted.mrn.lower()} ")

        assert response.status_code == 201, response.text
        [link] = await _links(db_session, patient.account_id)
        assert link.patient_id == wanted.id
        assert link.verified_via == "phone_dob_mrn"

    async def test_a_wrong_mrn_is_the_same_404_as_no_record(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        elsewhere = await insert_patient_record(db_session, hospital.id, phone=new_phone())

        for mrn in ("MRN-NOPE", elsewhere.mrn):
            response = await patient.link(hospital.id, mrn=mrn)
            assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _links(db_session, patient.account_id) == []

    async def test_a_deactivated_match_is_unavailable(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone, deleted=True)

        response = await patient.link(hospital.id)

        assert _error(response) == (403, "LINK_UNAVAILABLE", "Please contact the hospital.")
        assert await _links(db_session, patient.account_id) == []
        assert await _attempt_outcomes(db_session) == ["inactive_record"]

    async def test_an_active_match_is_preferred_to_a_deactivated_one(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone, deleted=True)
        active = await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id)

        assert response.status_code == 201, response.text
        [link] = await _links(db_session, patient.account_id)
        assert link.patient_id == active.id

    async def test_linking_again_is_idempotent(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        first = await patient.link(hospital.id)

        again = await patient.link(hospital.id)

        assert (first.status_code, again.status_code) == (201, 200)
        assert again.json()["data"] == first.json()["data"]
        assert len(await _links(db_session, patient.account_id)) == 1
        assert len(await _consents(db_session, patient.account_id)) == 1
        assert await _attempt_outcomes(db_session) == ["already_linked", "linked"]

    async def test_a_second_record_at_the_same_hospital_is_a_conflict(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        mine = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await insert_patient_record(
            db_session, hospital.id, phone=patient.phone, date_of_birth=OTHER_DOB
        )
        assert (await patient.link(hospital.id)).status_code == 201

        response = await patient.link(hospital.id, date_of_birth=OTHER_DOB.isoformat())

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        [link] = await _links(db_session, patient.account_id)
        assert link.patient_id == mine.id

    async def test_a_stale_link_from_another_account_is_replaced(
        self, application: FastAPI, sms: FakeSmsSender, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        """The hospital moved a record to a new number; the new number's owner links it."""
        async with (
            patient_client(application) as old_browser,
            patient_client(application) as new_browser,
        ):
            old_owner = await _signed_in(old_browser, sms)
            new_owner = await _signed_in(new_browser, sms)
            record = await insert_patient_record(db_session, hospital.id, phone=old_owner.phone)
            assert (await old_owner.link(hospital.id)).status_code == 201
            await db_session.execute(
                update(Patient).where(Patient.id == record.id).values(phone=new_owner.phone)
            )
            await db_session.commit()

            response = await new_owner.link(hospital.id)

            assert response.status_code == 201, response.text
            assert (await old_owner.me())["links"] == []
            assert [link["suspended"] for link in (await new_owner.me())["links"]] == [False]

        [stale] = await _links(db_session, old_owner.account_id)
        [fresh] = await _links(db_session, new_owner.account_id)
        assert stale.unlinked_at is not None
        assert stale.unlink_reason == "superseded"
        assert fresh.patient_id == record.id
        assert fresh.unlinked_at is None
        [ended] = await audit_rows(db_session, "patient.link.ended")
        assert ended.patient_account_id == new_owner.account_id
        assert ended.target_id == stale.id
        assert ended.context == {
            "reason": "superseded",
            "previous_account_id": str(old_owner.account_id),
        }

    async def test_an_account_can_relink_after_its_own_link_went_stale(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        """The hospital corrected which record carries this phone."""
        wrong = await insert_patient_record(
            db_session, hospital.id, phone=patient.phone, date_of_birth=OTHER_DOB
        )
        assert (
            await patient.link(hospital.id, date_of_birth=OTHER_DOB.isoformat())
        ).status_code == 201
        right = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await db_session.execute(
            update(Patient).where(Patient.id == wrong.id).values(phone=new_phone())
        )
        await db_session.commit()

        response = await patient.link(hospital.id)

        assert response.status_code == 201, response.text
        old, new = await _links(db_session, patient.account_id)
        assert (old.patient_id, old.unlink_reason) == (wrong.id, "superseded")
        assert (new.patient_id, new.unlinked_at) == (right.id, None)

    @pytest.mark.parametrize("state", ["flag_off", "flag_absent", "inactive", "unknown", "garbage"])
    async def test_a_hospital_that_cannot_be_used_is_the_same_404(
        self, patient: _Patient, db_session: AsyncSession, hospital_id: uuid.UUID, state: str
    ) -> None:
        await insert_patient_record(db_session, hospital_id, phone=patient.phone)
        reference: object = hospital_id
        if state == "flag_off":
            await open_hospital(db_session, hospital_id, enabled=False)
        elif state == "inactive":
            await open_hospital(db_session, hospital_id)
            await db_session.execute(
                update(Hospital).where(Hospital.id == hospital_id).values(is_active=False)
            )
            await db_session.commit()
        elif state == "unknown":
            reference = uuid.uuid4()
        elif state == "garbage":
            reference = "no such hospital!"

        response = await patient.link(reference)

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert await _links(db_session, patient.account_id) == []
        # Audited as a platform-level event: no hospital's staff can see it.
        [attempt] = await audit_rows(db_session, "patient.link.attempted")
        assert attempt.hospital_id is None
        assert attempt.patient_account_id == patient.account_id
        assert attempt.context == {
            "operation": "link",
            "outcome": "hospital_unavailable",
            "mrn_supplied": False,
        }

    async def test_a_record_at_another_hospital_is_never_matched(
        self,
        patient: _Patient,
        hospital: Hospital,
        other_hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        """The match runs inside the one hospital in the path."""
        theirs = await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)

        at_a = await patient.link(hospital.id)
        at_b = await patient.link(other_hospital.id)

        assert at_a.status_code == 404
        assert at_b.status_code == 201
        [link] = await _links(db_session, patient.account_id)
        assert (link.hospital_id, link.patient_id) == (other_hospital.id, theirs.id)

    async def test_an_account_can_be_linked_at_two_hospitals(
        self,
        patient: _Patient,
        hospital: Hospital,
        other_hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)

        assert (await patient.link(hospital.id)).status_code == 201
        assert (await patient.link(other_hospital.id)).status_code == 201

        links = (await patient.me())["links"]
        assert {link["hospital_id"] for link in links} == {str(hospital.id), str(other_hospital.id)}
        assert {link["hospital_name"] for link in links} == {hospital.name, other_hospital.name}

    async def test_a_stale_consent_version_is_refused(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.link(hospital.id, consent_policy_version="2025-01")

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert await _links(db_session, patient.account_id) == []
        assert await _consents(db_session, patient.account_id) == []

    @pytest.mark.parametrize(
        "body",
        [
            {"date_of_birth": "not-a-date"},
            {"consent_policy_version": ""},
            {"mrn": "M" * 31},
            {"hospital_id": str(uuid.uuid4())},
        ],
    )
    async def test_a_malformed_body_is_refused(
        self, patient: _Patient, hospital: Hospital, body: dict[str, Any]
    ) -> None:
        response = await patient.link(hospital.id, **body)

        assert response.status_code == 422, response.text

    async def test_five_failed_attempts_then_linking_is_unavailable(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, clock: ThrottleClock
    ) -> None:
        """Attack: work through dates of birth for a phone you hold."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        for day in range(LINK_ATTEMPTS):
            guess = (OTHER_DOB + timedelta(days=day)).isoformat()
            assert (await patient.link(hospital.id, date_of_birth=guess)).status_code == 404

        with_the_right_date = await patient.link(hospital.id)

        assert _error(with_the_right_date) == (
            403,
            "LINK_UNAVAILABLE",
            "Please contact the hospital.",
        )
        assert await _links(db_session, patient.account_id) == []
        assert (await _attempt_outcomes(db_session)).count("throttled") == 1

        clock.advance(minutes=12, seconds=1)
        assert (await patient.link(hospital.id)).status_code == 201

    async def test_a_successful_link_does_not_use_up_the_allowance(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        for _ in range(LINK_ATTEMPTS + 3):
            assert (await patient.link(hospital.id)).status_code in (200, 201)

    async def test_the_allowance_is_per_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        other_hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        await insert_patient_record(db_session, other_hospital.id, phone=patient.phone)
        for _ in range(LINK_ATTEMPTS):
            await patient.link(hospital.id)
        assert (await patient.link(hospital.id)).status_code == 403

        assert (await patient.link(other_hospital.id)).status_code == 201

    async def test_it_needs_a_patient_token(
        self, browser: AsyncClient, hospital: Hospital, actor_id: uuid.UUID
    ) -> None:
        body = {"date_of_birth": DOB.isoformat(), "consent_policy_version": POLICY}
        url = f"{PATIENT}/hospitals/{hospital.id}/link"

        assert (await browser.post(url, json=body)).status_code == 401
        staff = bearer(create_access_token(actor_id, hospital.id))
        assert (await browser.post(url, json=body, headers=staff)).status_code == 401


# ── The phone-binding rule, as /me shows it ──────────────────────────────────


class TestPhoneBinding:
    async def test_a_link_is_suspended_once_the_record_carries_another_phone(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)
        assert [link["suspended"] for link in (await patient.me())["links"]] == [False]

        await db_session.execute(
            update(Patient).where(Patient.id == record.id).values(phone=new_phone())
        )
        await db_session.commit()

        assert [link["suspended"] for link in (await patient.me())["links"]] == [True]
        # Evaluated at read time: nothing was written to the link.
        [link] = await _links(db_session, patient.account_id)
        assert link.unlinked_at is None

    async def test_a_link_is_suspended_while_the_record_is_deactivated(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        from datetime import UTC, datetime

        record = await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)

        await db_session.execute(
            update(Patient).where(Patient.id == record.id).values(deleted_at=datetime.now(UTC))
        )
        await db_session.commit()

        assert [link["suspended"] for link in (await patient.me())["links"]] == [True]

    async def test_a_link_at_a_hospital_that_closed_to_patients_is_not_shown(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)

        await open_hospital(db_session, hospital.id, enabled=False)

        assert (await patient.me())["links"] == []

    async def test_me_never_shows_another_accounts_links(
        self, application: FastAPI, sms: FakeSmsSender, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        async with patient_client(application) as one, patient_client(application) as two:
            linked = await _signed_in(one, sms)
            stranger = await _signed_in(two, sms)
            await insert_patient_record(db_session, hospital.id, phone=linked.phone)
            await linked.link(hospital.id)

            assert len((await linked.me())["links"]) == 1
            assert (await stranger.me())["links"] == []


# ── Register ─────────────────────────────────────────────────────────────────


class TestRegister:
    async def test_it_creates_a_record_on_the_verified_phone_and_links_it(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        response = await patient.register(hospital.id)

        assert response.status_code == 201, response.text
        data = response.json()["data"]
        assert set(data) == {"link", "profile"}
        assert data["link"]["hospital_id"] == str(hospital.id)
        assert data["link"]["suspended"] is False
        assert set(data["profile"]) == {"mrn", "first_name", "last_name", "date_of_birth", "gender"}
        assert data["profile"]["date_of_birth"] == DOB.isoformat()

        [record] = (
            (
                await db_session.execute(
                    select(Patient).where(
                        Patient.hospital_id == hospital.id, Patient.phone == patient.phone
                    )
                )
            )
            .scalars()
            .all()
        )
        assert record.mrn == data["profile"]["mrn"]
        assert (record.first_name, record.last_name) == ("Asha", "Verma")
        assert record.created_by is None  # no staff user created it
        [link] = await _links(db_session, patient.account_id)
        assert (link.patient_id, link.verified_via) == (record.id, "self_registration")

    async def test_the_client_cannot_choose_the_phone_or_the_mrn(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        for extra in (
            {"phone": new_phone()},
            {"mrn": "MRN-CHOSEN"},
            {"patient_id": str(uuid.uuid4())},
            {"email": "a@example.com"},
        ):
            response = await patient.register(hospital.id, **extra)
            assert response.status_code == 422, response.text

        assert await _links(db_session, patient.account_id) == []

    async def test_it_records_both_consents(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await patient.register(hospital.id)

        consents = await _consents(db_session, patient.account_id)

        assert {consent.purpose for consent in consents} == {
            "hospital_registration",
            "hospital_record_link",
        }
        assert all(consent.hospital_id == hospital.id for consent in consents)
        assert all(consent.policy_version == POLICY for consent in consents)

    async def test_it_is_audited(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(
            db_session, hospital.id, phone=patient.phone, date_of_birth=OTHER_DOB
        )

        response = await patient.register(hospital.id)

        assert response.status_code == 201, response.text
        [registered] = await audit_rows(db_session, "patient.record.registered")
        [created] = await audit_rows(db_session, "patient.link.created")
        assert registered.actor_type == created.actor_type == "patient"
        assert registered.patient_account_id == patient.account_id
        assert registered.hospital_id == hospital.id
        # For staff reviewing duplicates: another record already carried this phone.
        assert registered.context == {"other_records_on_phone": 1}
        assert created.context == {"verified_via": "self_registration"}
        # The existing patient service wrote its own event for the new row.
        assert len(await audit_rows(db_session, "patient.created")) == 1

    async def test_a_matching_record_must_be_linked_instead(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)

        response = await patient.register(hospital.id)

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert await _links(db_session, patient.account_id) == []
        assert await _consents(db_session, patient.account_id) == []
        assert await _attempt_outcomes(db_session) == ["record_exists"]

    async def test_a_deactivated_match_blocks_registration(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        """A deactivated record is not silently duplicated."""
        await insert_patient_record(db_session, hospital.id, phone=patient.phone, deleted=True)

        response = await patient.register(hospital.id)

        assert _error(response)[:2] == (403, "LINK_UNAVAILABLE")
        assert await _attempt_outcomes(db_session) == ["inactive_record"]

    async def test_an_account_registers_once_per_hospital(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        assert (await patient.register(hospital.id)).status_code == 201

        again = await patient.register(hospital.id, date_of_birth=OTHER_DOB.isoformat())

        assert _error(again)[:2] == (409, "RESOURCE_CONFLICT")
        records = (
            (await db_session.execute(select(Patient).where(Patient.phone == patient.phone)))
            .scalars()
            .all()
        )
        assert len(records) == 1

    async def test_an_account_already_linked_here_cannot_register(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        await insert_patient_record(db_session, hospital.id, phone=patient.phone)
        await patient.link(hospital.id)

        response = await patient.register(hospital.id, date_of_birth=OTHER_DOB.isoformat())

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")

    async def test_an_account_can_register_at_a_second_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        other_hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        assert (await patient.register(hospital.id)).status_code == 201
        assert (await patient.register(other_hospital.slug)).status_code == 201

        links = await _links(db_session, patient.account_id)
        assert {link.hospital_id for link in links} == {hospital.id, other_hospital.id}

    async def test_a_hospital_that_cannot_be_used_is_a_404(
        self, patient: _Patient, hospital_id: uuid.UUID, db_session: AsyncSession
    ) -> None:
        response = await patient.register(hospital_id)  # flag never set

        assert _error(response) == (404, "RESOURCE_NOT_FOUND", NO_MATCH)
        assert (
            await db_session.execute(select(Patient).where(Patient.phone == patient.phone))
        ).scalars().all() == []

    async def test_a_stale_consent_version_is_refused(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession
    ) -> None:
        response = await patient.register(hospital.id, consent_policy_version="2025-01")

        assert _error(response)[:2] == (409, "RESOURCE_CONFLICT")
        assert (
            await db_session.execute(select(Patient).where(Patient.phone == patient.phone))
        ).scalars().all() == []

    @pytest.mark.parametrize(
        "body",
        [
            {"first_name": "   "},
            {"last_name": ""},
            {"gender": "robot"},
            {"date_of_birth": "2999-01-01"},
            {"date_of_birth": "1800-01-01"},
            {"first_name": "A" * 101},
        ],
    )
    async def test_invalid_details_are_refused_and_nothing_is_created(
        self, patient: _Patient, hospital: Hospital, db_session: AsyncSession, body: dict[str, Any]
    ) -> None:
        response = await patient.register(hospital.id, **body)

        assert response.status_code == 422, response.text
        assert await _links(db_session, patient.account_id) == []
        assert await _consents(db_session, patient.account_id) == []

    async def test_registration_and_linking_share_one_allowance(
        self, patient: _Patient, hospital: Hospital
    ) -> None:
        for _ in range(LINK_ATTEMPTS):
            assert (await patient.link(hospital.id)).status_code == 404

        response = await patient.register(hospital.id)

        assert _error(response)[:2] == (403, "LINK_UNAVAILABLE")

    async def test_it_needs_a_patient_token(self, browser: AsyncClient, hospital: Hospital) -> None:
        response = await browser.post(
            f"{PATIENT}/hospitals/{hospital.id}/register",
            json={
                "first_name": "A",
                "last_name": "B",
                "date_of_birth": DOB.isoformat(),
                "gender": "female",
                "consent_policy_version": POLICY,
            },
        )

        assert response.status_code == 401

    async def test_after_registering_the_link_is_in_me(
        self, patient: _Patient, hospital: Hospital
    ) -> None:
        await patient.register(hospital.id)

        [link] = (await patient.me())["links"]

        assert link["hospital_id"] == str(hospital.id)
        assert link["suspended"] is False

    async def test_the_registered_record_is_staff_visible_in_that_hospital_only(
        self,
        patient: _Patient,
        hospital: Hospital,
        other_hospital: Hospital,
        db_session: AsyncSession,
    ) -> None:
        await patient.register(hospital.id)

        rows = (
            (await db_session.execute(select(Patient).where(Patient.phone == patient.phone)))
            .scalars()
            .all()
        )

        assert [row.hospital_id for row in rows] == [hospital.id]
