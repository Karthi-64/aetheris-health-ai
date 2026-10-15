"""A patient's own prescriptions and released lab results (§15, §16).

``docs/modules/15-patient-app.md`` §9, §15, §16 and §27.8. Read-only, in
every phase: nothing here writes a health record.

**Whose.** The authenticated account's, and nobody else's, by exactly the
rule :meth:`PatientAuthorization.own_contexts` already enforces for every
record endpoint: an honoured ``self`` link, at a hospital that is open to
patients, with the record-link consent in force. No request names a patient
or a hospital as ownership evidence; the ``(hospital_id, patient_id)`` pair
comes from the account's own links, and one that is somebody else's is the
same ``404`` as one that does not exist.

**What.** A prescription of every status is shown, labelled (§15). A lab
order that is not ``released`` does not exist (§16): the repository leans
towards refusal by default — it filters on ``status = released`` in both the
list query and the single-order read, so a pending, entered or cancelled
order cannot be read even by mistake. The DTOs are the patient-safe
allow-list of ``app/schemas/patient_app/records.py``; no staff or internal
field is ever mapped across.

A list spans every hospital where the caller's record is honoured, newest
first, paged in the database — the same shape as my appointments.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from app.core.exceptions import NotFoundError
from app.core.tenancy import TenantScope, cross_tenant, tenant_scope
from app.schemas.common import Page
from app.schemas.patient_app.records import (
    PatientLabItem,
    PatientLabResult,
    PatientPrescription,
    PatientPrescriptionDoctor,
    PatientPrescriptionItem,
    PatientRecordHospital,
)
from app.services.patient_app.errors import ConsentRequiredError

if TYPE_CHECKING:
    from app.models.doctor import Doctor
    from app.models.lab import LabOrder, LabOrderItem
    from app.models.patient_account import PatientAccount
    from app.models.pharmacy import Prescription, PrescriptionStatus
    from app.repositories.hospital_repository import HospitalDirectoryEntry
    from app.repositories.lab_order_repository import LabOrderRepository
    from app.repositories.prescription_repository import PrescriptionRepository
    from app.services.patient_app.hospital_gate import PatientHospitalGate
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "MAX_PAGE",
    "MAX_PAGE_SIZE",
    "PatientRecordsService",
]

_NOT_FOUND: Final = "Not Found"
#: A page of records (§9.2: lists are capped), and no list can be walked
#: without end.
DEFAULT_PAGE_SIZE: Final = 20
MAX_PAGE_SIZE: Final = 100
MAX_PAGE: Final = 1000

_OWN_PRESCRIPTIONS: Final = cross_tenant(
    "patient account: its own prescriptions, by its own honoured record links"
)
_OWN_LAB_ORDERS: Final = cross_tenant(
    "patient account: its own lab orders, by its own honoured record links"
)


def _reference(value: str) -> uuid.UUID:
    """A record reference, or the one refusal: a malformed one does not exist."""
    try:
        return uuid.UUID(value.strip().lower())
    except ValueError:
        raise NotFoundError(_NOT_FOUND) from None


def _doctor_view(doctor: Doctor) -> PatientPrescriptionDoctor:
    """A doctor as a patient may see them: name and specialisation, nothing else."""
    user = doctor.user
    name = " ".join(part for part in (user.first_name.strip(), user.last_name.strip()) if part)
    return PatientPrescriptionDoctor(
        ref=str(doctor.id),
        name=name or "Name withheld",
        specialization=doctor.specialization.strip(),
    )


def _prescription_view(
    prescription: Prescription, hospital: HospitalDirectoryEntry
) -> PatientPrescription:
    """One prescription as its patient may see it. Every field is set by name."""
    zone = ZoneInfo(hospital.timezone)
    return PatientPrescription(
        ref=str(prescription.id),
        status=prescription.status,
        prescribed_at=prescription.prescribed_at.astimezone(zone),
        timezone=hospital.timezone,
        hospital=PatientRecordHospital(ref=hospital.slug, name=hospital.name),
        doctor=_doctor_view(prescription.doctor),
        items=[
            PatientPrescriptionItem(
                medicine_name=item.medicine_name,
                dosage=item.dosage,
                frequency=item.frequency,
                duration_days=item.duration_days,
                instructions=item.instructions,
                quantity=item.quantity,
                quantity_dispensed=item.quantity_dispensed,
            )
            for item in prescription.items
        ],
    )


def _lab_view(order: LabOrder, hospital: HospitalDirectoryEntry) -> PatientLabResult:
    """One released lab order as its patient may see it. Every field is set by name."""
    zone = ZoneInfo(hospital.timezone)
    if order.released_at is None:
        # Fail closed: the query only returns released orders, so this cannot
        # happen — and refusing beats inventing a release time.
        raise NotFoundError(_NOT_FOUND)
    return PatientLabResult(
        ref=str(order.id),
        status=order.status,
        released_at=order.released_at.astimezone(zone),
        timezone=hospital.timezone,
        hospital=PatientRecordHospital(ref=hospital.slug, name=hospital.name),
        doctor=_doctor_view(order.doctor),
        items=[_lab_item_view(item, zone) for item in order.items],
    )


def _lab_item_view(item: LabOrderItem, zone: ZoneInfo) -> PatientLabItem:
    """One released result: the current value, and when it was amended, if it was."""
    amended_at = max(
        (amendment.amended_at for amendment in item.amendments if amendment is not None),
        default=None,
    )
    if item.released_at is None:
        # Fail closed: a released order's results are released with it, so
        # this cannot happen — and refusing beats inventing a release time.
        raise NotFoundError(_NOT_FOUND)
    return PatientLabItem(
        test_name=item.test_name,
        value=item.result_value or "",
        unit=item.result_unit,
        flag=item.result_flag,
        reference_low=item.reference_low,
        reference_high=item.reference_high,
        released_at=item.released_at.astimezone(zone),
        amended_on=amended_at.astimezone(zone).date() if amended_at is not None else None,
    )


class PatientRecordsService:
    """Lists and shows the authenticated patient's prescriptions and released
    lab results.

    :param authorization: Resolves the account's own records.
    :param gate: Describes the hospitals those records are at.
    :param prescriptions: Prescription data access.
    :param lab_orders: Lab order data access.
    """

    def __init__(
        self,
        authorization: PatientAuthorization,
        gate: PatientHospitalGate,
        prescriptions: PrescriptionRepository,
        lab_orders: LabOrderRepository,
    ) -> None:
        self._authorization = authorization
        self._gate = gate
        self._prescriptions = prescriptions
        self._lab_orders = lab_orders

    # ── Looks up the caller's own records ─────────────────────────────────────

    async def _own_hospitals(
        self, account: PatientAccount
    ) -> dict[uuid.UUID, tuple[HospitalDirectoryEntry, uuid.UUID]]:
        """The hospitals where the account's record is honoured, with that record's id."""
        hospitals: dict[uuid.UUID, tuple[HospitalDirectoryEntry, uuid.UUID]] = {}
        for context in await self._authorization.own_contexts(account):
            hospital = await self._gate.describe(str(context.hospital_id))
            if hospital is not None:
                hospitals[hospital.id] = (hospital, context.patient_id)
        return hospitals

    async def _context_for_ref(
        self, account: PatientAccount, hospital_ref: str
    ) -> tuple[HospitalDirectoryEntry, uuid.UUID]:
        """The hospital a reference names, if the account's record there is honoured.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's id or its code.
        :returns: The hospital to show, and the record id behind the link.
        :raises ConsentRequiredError: If a required platform policy is pending.
        :raises NotFoundError: If the hospital is unknown, closed to patients,
            or the account's link there is not honoured right now — one answer.
        """
        summary = await self._gate.resolve(hospital_ref)
        if summary is None:
            raise NotFoundError(_NOT_FOUND)
        try:
            context = await self._authorization.resolve_context(account, summary.id)
        except ConsentRequiredError:
            raise
        except NotFoundError:
            raise NotFoundError(_NOT_FOUND) from None
        hospital = await self._gate.describe(str(summary.id))
        if hospital is None:  # pragma: no cover — resolve_context already proved it open
            raise NotFoundError(_NOT_FOUND)
        return hospital, context.patient_id

    # ── Prescriptions ─────────────────────────────────────────────────────────

    async def list_prescriptions(
        self,
        account: PatientAccount,
        *,
        status: PrescriptionStatus | None = None,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[PatientPrescription]:
        """One page of the caller's prescriptions, across their honoured links.

        :param account: The authenticated, active account.
        :param status: Only prescriptions in this status, or all of them (§15:
            every status is shown, labelled).
        :param page: 1-based page number.
        :param page_size: Prescriptions per page.
        :returns: The page and the total, newest first.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        hospitals = await self._own_hospitals(account)
        rows, total = await self._prescriptions.list_for_patient_records(
            [(hospital.id, patient_id) for hospital, patient_id in hospitals.values()],
            _OWN_PRESCRIPTIONS,
            statuses=[status] if status is not None else None,
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        return Page[PatientPrescription](
            items=[_prescription_view(row, hospitals[row.hospital_id][0]) for row in rows],
            page=page,
            page_size=page_size,
            total_records=total,
        )

    async def get_prescription(
        self,
        account: PatientAccount,
        hospital_ref: str,
        prescription_ref: str,
    ) -> PatientPrescription:
        """One of the caller's own prescriptions, at the hospital named.

        Nothing says a prescription exists unless it is the caller's own: an
        unknown reference, a malformed one, a hospital closed to patients, a
        suspended link and somebody else's prescription are all the same
        ``404``.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's id or its code.
        :param prescription_ref: The prescription's reference.
        :raises ConsentRequiredError: If a required platform policy is pending.
        :raises NotFoundError: For every other refusal.
        """
        hospital, patient_id = await self._context_for_ref(account, hospital_ref)
        with tenant_scope(TenantScope.hospital(hospital.id)):
            prescription = await self._prescriptions.get_for_patient_records(
                [(hospital.id, patient_id)], hospital.id, _reference(prescription_ref)
            )
        if prescription is None:
            raise NotFoundError(_NOT_FOUND)
        return _prescription_view(prescription, hospital)

    # ── Lab results ───────────────────────────────────────────────────────────

    async def list_lab_results(
        self,
        account: PatientAccount,
        *,
        page: int = 1,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[PatientLabResult]:
        """One page of the caller's released lab results, across their links.

        Only ``released`` orders are in the query (§16) — nothing pending,
        entered, cancelled or somebody else's can appear.

        :param account: The authenticated, active account.
        :param page: 1-based page number.
        :param page_size: Orders per page.
        :returns: The page and the total, newest first.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        hospitals = await self._own_hospitals(account)
        rows, total = await self._lab_orders.list_for_patient_records(
            [(hospital.id, patient_id) for hospital, patient_id in hospitals.values()],
            _OWN_LAB_ORDERS,
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        return Page[PatientLabResult](
            items=[_lab_view(row, hospitals[row.hospital_id][0]) for row in rows],
            page=page,
            page_size=page_size,
            total_records=total,
        )

    async def get_lab_result(
        self,
        account: PatientAccount,
        hospital_ref: str,
        order_ref: str,
    ) -> PatientLabResult:
        """One of the caller's own released lab orders, at the hospital named.

        An order that is not released is exactly as invisible as one that
        does not exist (§16), and so is a cancelled one, and one that is
        somebody else's.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's id or its code.
        :param order_ref: The lab order's reference.
        :raises ConsentRequiredError: If a required platform policy is pending.
        :raises NotFoundError: For every other refusal.
        """
        hospital, patient_id = await self._context_for_ref(account, hospital_ref)
        with tenant_scope(TenantScope.hospital(hospital.id)):
            order = await self._lab_orders.get_for_patient_records(
                [(hospital.id, patient_id)],
                hospital.id,
                _reference(order_ref),
            )
        if order is None:
            raise NotFoundError(_NOT_FOUND)
        return _lab_view(order, hospital)
