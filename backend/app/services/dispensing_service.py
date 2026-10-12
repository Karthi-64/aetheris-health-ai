"""Business logic for prescriptions and dispensing.

The therapeutic loop of ``docs/modules/08-pharmacy.md`` §5.1: a doctor
prescribes in a visit, a pharmacist dispenses, stock comes off the shelf and a
line goes on the bill.

What the service guarantees:

- **First to expire, first out** (business rule 2). The server picks the
  batches; a client cannot.
- **All or nothing** (rule 3, AC-3). Every batch a dispense will draw on is
  locked and the whole allocation worked out *before* anything is written. If
  any medicine is short, the request fails having changed nothing.
- **Nothing expired or recalled leaves the shelf** (rule 4, AC-5). A batch
  expiring today is still dispensable (§14); the response warns about any
  batch within thirty days of expiry.
- **A partial dispense says why** (rule 5).
- **Every dispense is billed** (rule 6), in the same transaction, through the
  :class:`~app.core.charges.ChargeSink` seam.

Locks are always taken in the same order — the prescription, then batches by
medicine id and expiry — so two concurrent dispenses cannot deadlock.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.audit import AuditEvent
from app.core.charges import Charge, ChargeSink, NullChargeSink
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError
from app.core.logging import get_logger
from app.models.appointment import AppointmentStatus
from app.models.pharmacy import DISPENSABLE_STATUSES, PrescriptionStatus, StockMovementReason
from app.schemas.common import Page, PaginationParams
from app.schemas.pharmacy import (
    CancelPrescriptionRequest,
    CreatePrescriptionRequest,
    DispenseRequest,
    DispenseResponse,
    PrescriptionResponse,
)
from app.services.pharmacy_common import field_error, hospital_today

if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import date

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.pharmacy import Medicine, MedicineBatch, Prescription, PrescriptionItem
    from app.repositories.appointment_repository import AppointmentRepository
    from app.repositories.hospital_repository import HospitalRepository
    from app.repositories.medicine_repository import MedicineRepository
    from app.repositories.prescription_repository import PrescriptionRepository

logger = get_logger(__name__)

__all__ = [
    "DispensingService",
    "InsufficientStockError",
    "PrescriptionNotFoundError",
    "PrescriptionStateError",
]

#: Appointment statuses a prescription cannot be written against.
_UNPRESCRIBABLE_VISITS = frozenset({AppointmentStatus.CANCELLED, AppointmentStatus.NO_SHOW})


# ── Errors ──────────────────────────────────────────────────────────────────


class PrescriptionNotFoundError(NotFoundError):
    """Raised when a prescription is absent from the requested hospital."""

    def __init__(self, prescription_id: uuid.UUID) -> None:
        super().__init__(
            message="Prescription not found.", detail={"prescription_id": str(prescription_id)}
        )


class PrescriptionStateError(BusinessRuleError):
    """Raised when a step is not allowed from the prescription's status."""

    def __init__(self, action: str, status: PrescriptionStatus) -> None:
        super().__init__(
            message=f"Cannot {action} a prescription that is {status.value.replace('_', ' ')}.",
            detail={"status": status.value},
        )


class InsufficientStockError(ConflictError):
    """Raised when a medicine cannot be dispensed in full (rule 3, AC-3).

    A 409 rather than a 400: the request was valid, and would succeed against
    a fuller shelf. Nothing has been written when this is raised.
    """

    def __init__(self, shortages: list[dict[str, Any]]) -> None:
        names = ", ".join(str(shortage["medicine"]) for shortage in shortages)
        super().__init__(
            message=f"Not enough stock to dispense: {names}. Nothing was dispensed.",
            detail={"shortages": shortages},
        )


@dataclass(frozen=True, slots=True)
class _Take:
    """Units to take from one batch for one prescription line."""

    item: PrescriptionItem
    medicine: Medicine
    batch: MedicineBatch
    quantity: int


# ── Service ─────────────────────────────────────────────────────────────────


class DispensingService:
    """Prescribing and dispensing.

    :param prescriptions: Prescription and dispense data access.
    :param medicines: Medicine, batch and ledger data access.
    :param appointments: Appointment lookups, for the visit a prescription hangs off.
    :param hospitals: Hospital lookups, for the local date expiry is judged by.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    :param charges: Where dispensed medicines are charged. Optional; a service
        built without one charges nothing.
    """

    def __init__(
        self,
        prescriptions: PrescriptionRepository,
        medicines: MedicineRepository,
        appointments: AppointmentRepository,
        hospitals: HospitalRepository,
        session: AsyncSession,
        audit: AuditSink,
        charges: ChargeSink | None = None,
    ) -> None:
        self._prescriptions = prescriptions
        self._medicines = medicines
        self._appointments = appointments
        self._hospitals = hospitals
        self._session = session
        self._audit = audit
        self._charges: ChargeSink = charges or NullChargeSink()

    # ── Prescribing ───────────────────────────────────────────────────────────

    async def create_prescription(
        self,
        hospital_id: uuid.UUID,
        payload: CreatePrescriptionRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PrescriptionResponse:
        """Write a prescription for the patient of a visit.

        The patient and the prescribing doctor are read from the appointment.
        A catalog line takes its name from the medicine as it stands now; a
        free-text line carries its own and can never be dispensed here.

        :raises ValidationError: If the appointment or a medicine is unknown in
            this hospital, the visit was cancelled or missed, or a medicine is
            inactive.
        """
        appointment = await self._appointments.get_appointment_by_id(
            hospital_id, payload.appointment_id
        )
        if appointment is None:
            raise field_error("appointment_id", "Appointment not found in this hospital.")
        if appointment.status in _UNPRESCRIBABLE_VISITS:
            raise field_error(
                "appointment_id",
                f"A prescription cannot be written for a visit that is {appointment.status.value}.",
            )

        wanted = [line.medicine_id for line in payload.items if line.medicine_id is not None]
        catalog = {
            medicine.id: medicine
            for medicine in await self._medicines.get_medicines_by_ids(hospital_id, wanted)
        }
        items: list[dict[str, Any]] = []
        for index, line in enumerate(payload.items):
            name = line.medicine_name
            if line.medicine_id is not None:
                medicine = catalog.get(line.medicine_id)
                if medicine is None:
                    raise field_error(
                        f"items.{index}.medicine_id", "Medicine not found in this hospital."
                    )
                if not medicine.is_active:
                    raise field_error(
                        f"items.{index}.medicine_id",
                        f"Medicine '{medicine.sku}' is inactive and cannot be prescribed.",
                    )
                name = " ".join(part for part in (medicine.name, medicine.strength) if part)
            items.append(
                {
                    "medicine_id": line.medicine_id,
                    "medicine_name": name,
                    "dosage": line.dosage,
                    "frequency": line.frequency,
                    "duration_days": line.duration_days,
                    "instructions": line.instructions,
                    "quantity": line.quantity,
                }
            )

        prescription = await self._prescriptions.create_prescription(
            hospital_id=hospital_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            doctor_id=appointment.doctor_id,
            prescribed_at=datetime.now(UTC),
            notes=payload.notes,
            items=items,
            created_by=actor_id,
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.prescription.created",
                hospital_id=hospital_id,
                target_type="prescription",
                target_id=prescription.id,
                actor_id=actor_id,
                context={
                    "appointment_id": str(appointment.id),
                    "line_count": len(items),
                    "free_text_lines": sum(1 for item in items if item["medicine_id"] is None),
                },
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.prescription.created",
            hospital_id=str(hospital_id),
            prescription_id=str(prescription.id),
            line_count=len(items),
        )
        return await self._respond(prescription)

    async def cancel_prescription(
        self,
        hospital_id: uuid.UUID,
        prescription_id: uuid.UUID,
        payload: CancelPrescriptionRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> PrescriptionResponse:
        """Cancel a prescription nothing has been dispensed from.

        :raises PrescriptionNotFoundError: If absent from this tenant.
        :raises PrescriptionStateError: If any of it has been dispensed, or it
            is already cancelled.
        """
        prescription = await self._lock_or_raise(hospital_id, prescription_id)
        if prescription.status is not PrescriptionStatus.ACTIVE:
            raise PrescriptionStateError("cancel", prescription.status)

        prescription = await self._prescriptions.update_prescription(
            prescription,
            updated_by=actor_id,
            status=PrescriptionStatus.CANCELLED,
            cancelled_at=datetime.now(UTC),
            cancel_reason=payload.reason,
        )
        await self._audit.record(
            AuditEvent(
                action="pharmacy.prescription.cancelled",
                hospital_id=hospital_id,
                target_type="prescription",
                target_id=prescription.id,
                actor_id=actor_id,
                changes={"status": {"before": "active", "after": "cancelled"}},
                context={"reason": payload.reason},
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.prescription.cancelled",
            hospital_id=str(hospital_id),
            prescription_id=str(prescription.id),
        )
        return await self._respond(prescription)

    # ── Dispensing ────────────────────────────────────────────────────────────

    async def dispense(
        self,
        hospital_id: uuid.UUID,
        prescription_id: uuid.UUID,
        payload: DispenseRequest | None = None,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DispenseResponse:
        """Dispense a prescription, in full or in part (module spec §5.1).

        :param hospital_id: The tenant to scope to.
        :param prescription_id: The prescription to dispense.
        :param payload: Which lines and how much. ``None``, or no ``items``,
            dispenses everything outstanding.
        :param actor_id: UUID of the pharmacist.
        :returns: The dispense, with the batches drawn on and any warnings.
        :raises PrescriptionNotFoundError: If absent from this tenant.
        :raises PrescriptionStateError: If it is fully dispensed or cancelled.
        :raises ValidationError: If a line is not on the prescription, is free
            text, asks for more than is outstanding (§11), or the dispense is
            partial and gives no reason (rule 5).
        :raises BusinessRuleError: If there is nothing left to dispense, or a
            medicine has been deactivated.
        :raises InsufficientStockError: If any medicine is short. Nothing is
            written.
        """
        request = payload or DispenseRequest()
        prescription = await self._lock_or_raise(hospital_id, prescription_id)
        if prescription.status not in DISPENSABLE_STATUSES:
            raise PrescriptionStateError("dispense", prescription.status)

        wanted = self._resolve_lines(prescription, request)
        self._require_reason_if_partial(prescription, wanted, request)

        today = await hospital_today(self._hospitals, hospital_id)
        takes = await self._allocate(hospital_id, wanted, today=today)

        # Everything is locked and the allocation is complete. Only now is
        # anything written, so a shortage above left the database untouched.
        now = datetime.now(UTC)
        lines: list[dict[str, Any]] = [
            {
                "prescription_item_id": take.item.id,
                "medicine_id": take.medicine.id,
                "batch_id": take.batch.id,
                "quantity": take.quantity,
                "unit_price": take.medicine.unit_price,
                "total": take.medicine.unit_price * take.quantity,
            }
            for take in takes
        ]
        total = sum((take.medicine.unit_price * take.quantity for take in takes), Decimal("0.00"))
        dispense = await self._prescriptions.create_dispense(
            prescription=prescription,
            dispensed_at=now,
            dispensed_by=actor_id,
            total_amount=total,
            notes=request.notes,
            lines=lines,
        )
        for take in takes:
            await self._medicines.apply_movement(
                take.batch,
                quantity_change=-take.quantity,
                reason=StockMovementReason.DISPENSED,
                moved_at=now,
                moved_by=actor_id,
                reference_type="dispense",
                reference_id=dispense.id,
            )
        for item, quantity in wanted:
            await self._prescriptions.record_dispensed(item, quantity, updated_by=actor_id)

        medicines = {take.medicine.id: take.medicine for take in takes}
        invoice_id = await self._charges.add_charges(
            hospital_id,
            patient_id=prescription.patient_id,
            appointment_id=prescription.appointment_id,
            charges=[
                Charge(
                    description=f"Medicine — {item.medicine_name}",
                    unit_price=medicines[item.medicine_id].unit_price,  # type: ignore[index]
                    quantity=Decimal(quantity),
                )
                for item, quantity in wanted
            ],
            source="pharmacy",
            actor_id=actor_id,
        )
        if invoice_id is not None:
            dispense = await self._prescriptions.set_dispense_invoice(dispense, invoice_id)

        before = prescription.status
        prescription = await self._prescriptions.update_prescription(
            prescription,
            updated_by=actor_id,
            status=(
                PrescriptionStatus.DISPENSED
                if self._fully_dispensed(prescription)
                else PrescriptionStatus.PARTIALLY_DISPENSED
            ),
        )

        await self._audit.record(
            AuditEvent(
                action="pharmacy.dispensed",
                hospital_id=hospital_id,
                target_type="prescription",
                target_id=prescription.id,
                actor_id=actor_id,
                changes={"status": {"before": before.value, "after": prescription.status.value}},
                context={
                    "dispense_id": str(dispense.id),
                    "total_amount": str(total),
                    "invoice_id": str(invoice_id) if invoice_id else None,
                    "lines": [
                        {
                            "medicine": take.medicine.sku,
                            "batch": take.batch.batch_number,
                            "quantity": take.quantity,
                        }
                        for take in takes
                    ],
                },
            )
        )
        await self._session.commit()
        logger.info(
            "pharmacy.dispensed",
            hospital_id=str(hospital_id),
            prescription_id=str(prescription.id),
            dispense_id=str(dispense.id),
            line_count=len(takes),
            status=prescription.status.value,
        )
        return DispenseResponse.from_model(dispense, today=today)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_prescription(
        self, hospital_id: uuid.UUID, prescription_id: uuid.UUID
    ) -> PrescriptionResponse:
        """Retrieve one prescription, with what is in stock for each line.

        :raises PrescriptionNotFoundError: If absent from this tenant.
        """
        prescription = await self._prescriptions.get_prescription_by_id(
            hospital_id, prescription_id
        )
        if prescription is None:
            raise PrescriptionNotFoundError(prescription_id)
        return await self._respond(prescription)

    async def list_prescriptions(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        pending_only: bool = False,
        status: PrescriptionStatus | None = None,
        patient_id: uuid.UUID | None = None,
        doctor_id: uuid.UUID | None = None,
        appointment_id: uuid.UUID | None = None,
    ) -> Page[PrescriptionResponse]:
        """List prescriptions.

        :param pending_only: The dispensing queue (§12): only prescriptions
            with something left to dispense, longest-waiting first.
        :param status: Only prescriptions in this status. Ignored with
            ``pending_only``.
        :returns: One page of prescriptions plus the total count.
        """
        page_params = pagination or PaginationParams()
        statuses: Sequence[PrescriptionStatus] | None = (
            tuple(DISPENSABLE_STATUSES) if pending_only else ((status,) if status else None)
        )
        filters: dict[str, Any] = {
            "statuses": statuses,
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "appointment_id": appointment_id,
        }
        rows = await self._prescriptions.list_prescriptions(
            hospital_id,
            skip=page_params.offset,
            limit=page_params.limit,
            oldest_first=pending_only,
            **filters,
        )
        total = await self._prescriptions.count_prescriptions(hospital_id, **filters)

        today = await hospital_today(self._hospitals, hospital_id)
        medicine_ids = list(
            {item.medicine_id for row in rows for item in row.items if item.medicine_id is not None}
        )
        availability = await self._medicines.dispensable_totals(hospital_id, medicine_ids, on=today)
        return Page[PrescriptionResponse](
            items=[PrescriptionResponse.from_model(row, availability=availability) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def list_dispenses(
        self, hospital_id: uuid.UUID, prescription_id: uuid.UUID
    ) -> list[DispenseResponse]:
        """Return the dispenses made against a prescription, oldest first.

        :raises PrescriptionNotFoundError: If absent from this tenant.
        """
        if await self._prescriptions.get_prescription_by_id(hospital_id, prescription_id) is None:
            raise PrescriptionNotFoundError(prescription_id)
        today = await hospital_today(self._hospitals, hospital_id)
        dispenses = await self._prescriptions.list_dispenses(hospital_id, prescription_id)
        return [DispenseResponse.from_model(dispense, today=today) for dispense in dispenses]

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _lock_or_raise(
        self, hospital_id: uuid.UUID, prescription_id: uuid.UUID
    ) -> Prescription:
        """Lock a prescription or raise :class:`PrescriptionNotFoundError`."""
        prescription = await self._prescriptions.get_prescription_by_id(
            hospital_id, prescription_id, for_update=True
        )
        if prescription is None:
            raise PrescriptionNotFoundError(prescription_id)
        return prescription

    async def _respond(self, prescription: Prescription) -> PrescriptionResponse:
        """Build a prescription response with current stock for each line."""
        today = await hospital_today(self._hospitals, prescription.hospital_id)
        medicine_ids = [
            item.medicine_id for item in prescription.items if item.medicine_id is not None
        ]
        availability = await self._medicines.dispensable_totals(
            prescription.hospital_id, medicine_ids, on=today
        )
        return PrescriptionResponse.from_model(prescription, availability=availability)

    @staticmethod
    def _fully_dispensed(prescription: Prescription) -> bool:
        """Whether every line the pharmacy can fill has been filled.

        Free-text lines are for medicines bought elsewhere; they never hold a
        prescription open.
        """
        return all(
            item.quantity_remaining == 0
            for item in prescription.items
            if item.medicine_id is not None
        )

    @staticmethod
    def _resolve_lines(
        prescription: Prescription, request: DispenseRequest
    ) -> list[tuple[PrescriptionItem, int]]:
        """Work out which lines to dispense and how much of each.

        :returns: ``(item, quantity)`` pairs, in prescription order.
        :raises ValidationError: If a named line is not on the prescription, is
            free text, or asks for more than is outstanding.
        :raises BusinessRuleError: If nothing is left to dispense.
        """
        if request.items is None:
            wanted = [
                (item, item.quantity_remaining)
                for item in prescription.items
                if item.medicine_id is not None and item.quantity_remaining > 0
            ]
            if not wanted:
                msg = "Nothing on this prescription is left to dispense."
                raise BusinessRuleError(msg)
            return wanted

        by_id = {item.id: item for item in prescription.items}
        asked: dict[uuid.UUID, int] = {}
        for index, line in enumerate(request.items):
            item = by_id.get(line.prescription_item_id)
            if item is None:
                raise field_error(
                    f"items.{index}.prescription_item_id", "Not an item on this prescription."
                )
            if item.medicine_id is None:
                raise field_error(
                    f"items.{index}.prescription_item_id",
                    f"'{item.medicine_name}' is a free-text line and is not stocked here.",
                )
            if line.quantity > item.quantity_remaining:
                raise field_error(
                    f"items.{index}.quantity",
                    f"Only {item.quantity_remaining} of {item.medicine_name} is left to dispense.",
                )
            asked[item.id] = line.quantity
        return [(item, asked[item.id]) for item in prescription.items if item.id in asked]

    @staticmethod
    def _require_reason_if_partial(
        prescription: Prescription,
        wanted: list[tuple[PrescriptionItem, int]],
        request: DispenseRequest,
    ) -> None:
        """Business rule 5: a dispense that leaves something outstanding says why."""
        taking = {item.id: quantity for item, quantity in wanted}
        leaves_something = any(
            item.quantity_remaining - taking.get(item.id, 0) > 0
            for item in prescription.items
            if item.medicine_id is not None
        )
        if leaves_something and request.notes is None:
            raise field_error("notes", "A partial dispense needs a reason. Say why in the notes.")

    async def _allocate(
        self,
        hospital_id: uuid.UUID,
        wanted: list[tuple[PrescriptionItem, int]],
        *,
        today: date,
    ) -> list[_Take]:
        """Lock the batches needed and decide what to take from each.

        Medicines are locked in id order and batches in expiry order, so two
        dispenses always meet the same locks in the same sequence. Writes
        nothing.

        :returns: One :class:`_Take` per batch drawn on, earliest expiry first
            within each prescription line.
        :raises BusinessRuleError: If a medicine has been deactivated.
        :raises InsufficientStockError: If any medicine is short.
        """
        medicine_ids = sorted({item.medicine_id for item, _ in wanted if item.medicine_id})
        medicines = {
            medicine.id: medicine
            for medicine in await self._medicines.get_medicines_by_ids(hospital_id, medicine_ids)
        }
        shelves: dict[uuid.UUID, list[MedicineBatch]] = {}
        left: dict[uuid.UUID, int] = {}
        for medicine_id in medicine_ids:
            medicine = medicines.get(medicine_id)
            if medicine is None or not medicine.is_active:
                msg = "A prescribed medicine has been deactivated and cannot be dispensed."
                raise BusinessRuleError(msg, detail={"medicine_id": str(medicine_id)})
            shelves[medicine_id] = await self._medicines.lock_dispensable_batches(
                hospital_id, medicine_id, on=today
            )
            for batch in shelves[medicine_id]:
                left[batch.id] = batch.quantity_on_hand

        takes: list[_Take] = []
        shortages: list[dict[str, Any]] = []
        for item, quantity in wanted:
            assert item.medicine_id is not None  # _resolve_lines excludes free text
            needed = quantity
            for batch in shelves[item.medicine_id]:
                if needed == 0:
                    break
                taken = min(needed, left[batch.id])
                if taken > 0:
                    takes.append(_Take(item, medicines[item.medicine_id], batch, taken))
                    left[batch.id] -= taken
                    needed -= taken
            if needed > 0:
                shortages.append(
                    {
                        "prescription_item_id": str(item.id),
                        "medicine": item.medicine_name,
                        "requested": quantity,
                        "available": quantity - needed,
                    }
                )
        if shortages:
            raise InsufficientStockError(shortages)
        return takes
