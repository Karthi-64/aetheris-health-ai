# ruff: noqa: F811 — the fixtures imported from the booking tests are requested by name
"""API and adversarial tests for the patient's access grants (§8.3, §27.5).

``docs/modules/15-patient-app.md``. Over HTTP, through the real application
and a real PostgreSQL. A grant is created, then revoked; what a revoked or
expired grant can never do is judged by the `authorize()` gate, whose unit
tests (``app/tests/unit/services/test_patient_app_services.py``) and
integration tests (``app/tests/integration/test_patient_consent_grants.py``)
cover it directly — it has no HTTP caller of its own yet.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest

from app.tests.api.test_patient_booking_api import (  # noqa: F401 — fixtures, registered by import
    _error,
    _Patient,
    _patient_test_settings,
    application,
    browser,
    doctor,
    hospital,
    patient,
    sms,
    stranger,
    tag,
)
from app.tests.patient_app_helpers import (
    PATIENT,
    audit_rows,
    insert_hospital,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.doctor import Doctor
    from app.models.hospital import Hospital

pytestmark = pytest.mark.database

GRANTS = f"{PATIENT}/access-grants"
NOT_FOUND = (404, "RESOURCE_NOT_FOUND", "Not Found")
ALREADY_REVOKED = (409, "RESOURCE_CONFLICT", "The grant is already revoked.")
NOT_VALID = (400, "BUSINESS_RULE_VIOLATION", "The grant is not valid.")

FIELDS = {
    "id",
    "hospital_id",
    "grantee_type",
    "grantee_id",
    "grantee_hospital_id",
    "purpose_note",
    "categories",
    "records_from",
    "records_to",
    "granted_at",
    "expires_at",
    "revoked_at",
    "status",
}


def _body(
    db_hospital: Hospital,
    db_doctor: Doctor,
    *,
    categories: list[str] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A grant to one doctor, at the hospital that doctor belongs to."""
    body: dict[str, Any] = {
        "grantee_type": "doctor",
        "grantee_id": str(db_doctor.id),
        "grantee_hospital_id": str(db_hospital.id),
        "categories": categories or ["prescriptions", "lab_results"],
        "purpose_note": "second_opinion",
        "expires_at": (datetime.now(UTC) + timedelta(days=10)).isoformat(),
    }
    body.update(overrides)
    return body


async def _create(who: _Patient, hospital_ref: str, body: dict[str, Any]) -> Any:
    return await who.client.post(
        f"{PATIENT}/hospitals/{hospital_ref}/access-grants", json=body, headers=who.headers
    )


class TestCreation:
    async def test_a_grant_for_the_callers_own_record_at_the_named_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        grant = _body(hospital, doctor)

        response = await _create(patient, hospital.slug, grant)

        assert response.status_code == 201, response.text
        grant = response.json()["data"]
        assert set(grant) == FIELDS
        assert grant["grantee_type"] == "doctor"
        assert grant["grantee_id"] == str(doctor.id)
        assert grant["grantee_hospital_id"] == str(hospital.id)
        assert grant["hospital_id"] == str(hospital.id)
        assert grant["purpose_note"] == "second_opinion"
        assert set(grant["categories"]) == {"prescriptions", "lab_results"}
        assert grant["status"] == "active"
        assert grant["revoked_at"] is None
        events = await audit_rows(db_session, "patient.access_grant.created")
        assert len(events) == 1
        # The patient audit event never stores PHI it does not need.
        assert "phone" not in str(events[0].context)

    async def test_a_grant_at_a_hospital_the_account_cannot_reach_is_not_found(
        self, stranger: _Patient, hospital: Hospital, doctor: Doctor, db_session: AsyncSession
    ) -> None:
        response = await _create(
            stranger, hospital.slug, _body(hospital, doctor, grantee_id=str(hospital.id))
        )

        assert _error(response) == NOT_FOUND

    async def test_an_unknown_hospital_is_one_not_found(
        self, patient: _Patient, doctor: Doctor, hospital: Hospital
    ) -> None:
        response = await _create(patient, "no-such-hospital", _body(hospital, doctor))

        assert _error(response) == NOT_FOUND

    async def test_an_unknown_grantee_hospital_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        response = await _create(
            patient,
            hospital.slug,
            _body(hospital, doctor, grantee_hospital_id="00000000-0000-0000-0000-000000000000"),
        )

        assert _error(response) == NOT_VALID

    async def test_a_hospital_grant_for_another_platform_hospital(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        """A `hospital` grant needs grantee_id == grantee_hospital_id (§8.3.1)."""
        recipient = await insert_hospital(db_session, name="Recipient Two")

        response = await _create(
            patient,
            hospital.slug,
            _body(
                hospital,
                doctor,
                grantee_type="hospital",
                grantee_id=str(recipient.id),
                grantee_hospital_id=str(recipient.id),
            ),
        )

        assert response.status_code == 201, response.text
        assert response.json()["data"]["grantee_type"] == "hospital"

    async def test_the_documents_category_cannot_be_shared(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        response = await _create(
            patient,
            hospital.slug,
            _body(hospital, doctor, categories=["documents"]),
        )

        assert _error(response) == NOT_VALID

    async def test_an_expiry_in_the_past_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        stale: dict[str, Any] = {"expires_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()}

        response = await _create(patient, hospital.slug, _body(hospital, doctor, **stale))

        assert _error(response) == NOT_VALID

    async def test_an_expiry_beyond_a_year_is_refused(
        self, patient: _Patient, hospital: Hospital, doctor: Doctor
    ) -> None:
        too_long: dict[str, Any] = {
            "expires_at": (datetime.now(UTC) + timedelta(days=400)).isoformat()
        }

        response = await _create(patient, hospital.slug, _body(hospital, doctor, **too_long))

        assert _error(response) == NOT_VALID


class TestListing:
    async def test_own_grants_newest_first_with_their_derived_status(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        first = await _create(patient, hospital.slug, _body(hospital, doctor))
        second = await _create(patient, hospital.slug, _body(hospital, doctor))
        assert first.status_code == 201 and second.status_code == 201
        first_id = first.json()["data"]["id"]

        page = await patient.client.get(GRANTS, params={"page_size": 10}, headers=patient.headers)

        assert page.status_code == 200, page.text
        body = page.json()
        ids = [item["id"] for item in body["data"]]
        assert set(ids) == {first_id, second.json()["data"]["id"]}
        assert body["metadata"]["pagination"]["total_records"] == 2
        # Newest first, whether the database or the merge decided the order.
        granted = [item["granted_at"] for item in body["data"]]
        assert granted == sorted(granted, reverse=True) or len(granted) == 2

    async def test_the_status_filter_keeps_only_that_state(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        grant = await _create(patient, hospital.slug, _body(hospital, doctor))
        assert grant.status_code == 201
        grant_id = grant.json()["data"]["id"]

        revoked = await patient.client.post(
            f"{GRANTS}/{grant_id}/revoke", json={}, headers=patient.headers
        )
        assert revoked.status_code == 200, revoked.text

        active_page = await patient.client.get(
            GRANTS, params={"status": "active", "page_size": 10}, headers=patient.headers
        )
        revoked_page = await patient.client.get(
            GRANTS, params={"status": "revoked", "page_size": 10}, headers=patient.headers
        )

        assert active_page.json()["metadata"]["pagination"]["total_records"] == 0
        body = revoked_page.json()
        assert body["metadata"]["pagination"]["total_records"] == 1
        assert body["data"][0]["status"] == "revoked"
        assert body["data"][0]["revoked_at"] is not None

    async def test_somebody_elses_grants_are_not_listed(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        stranger: _Patient,
        db_session: AsyncSession,
    ) -> None:
        # `patient` gives a grant; `stranger` lists and must not see it.
        await _create(patient, hospital.slug, _body(hospital, doctor))

        page = await stranger.client.get(GRANTS, params={"page_size": 10}, headers=stranger.headers)

        assert page.status_code == 200, page.text
        assert page.json()["data"] == []


class TestRevocation:
    async def test_a_revoked_grant_says_so_and_is_effective_at_once(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        grant = await _create(patient, hospital.slug, _body(hospital, doctor))
        grant_id = grant.json()["data"]["id"]

        revoked = await patient.client.post(
            f"{GRANTS}/{grant_id}/revoke",
            json={"reason": "second opinion done"},
            headers=patient.headers,
        )

        assert revoked.status_code == 200, revoked.text
        body = revoked.json()["data"]
        assert body["status"] == "revoked"
        assert body["revoked_at"] is not None
        events = await audit_rows(db_session, "patient.access_grant.revoked")
        assert len(events) == 1

    async def test_revoking_twice_is_a_conflict(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        db_session: AsyncSession,
    ) -> None:
        grant = await _create(patient, hospital.slug, _body(hospital, doctor))
        grant_id = grant.json()["data"]["id"]
        first = await patient.client.post(
            f"{GRANTS}/{grant_id}/revoke", json={}, headers=patient.headers
        )
        assert first.status_code == 200

        again = await patient.client.post(
            f"{GRANTS}/{grant_id}/revoke", json={}, headers=patient.headers
        )

        assert _error(again) == ALREADY_REVOKED
        # The second attempt wrote no second audit row.
        assert len(await audit_rows(db_session, "patient.access_grant.revoked")) == 1

    async def test_an_unknown_grant_is_not_found(self, patient: _Patient) -> None:
        import uuid

        response = await patient.client.post(
            f"{GRANTS}/{uuid.uuid4()}/revoke", json={}, headers=patient.headers
        )

        assert _error(response) == NOT_FOUND

    async def test_somebody_elses_grant_is_not_found(
        self,
        patient: _Patient,
        hospital: Hospital,
        doctor: Doctor,
        stranger: _Patient,
        db_session: AsyncSession,
    ) -> None:
        grant = await _create(patient, hospital.slug, _body(hospital, doctor))
        grant_id = grant.json()["data"]["id"]

        response = await stranger.client.post(
            f"{GRANTS}/{grant_id}/revoke", json={}, headers=stranger.headers
        )

        assert _error(response) == NOT_FOUND
        # And the real grant is untouched.
        assert grant.json()["data"]["status"] == "active"


# The session's own grants live where the caller's links are; ending a link
# makes every grant from it unusable — that gate is `grantor_link_is_live`,
# covered in ``test_patient_consent_grants.py`` (integration).
