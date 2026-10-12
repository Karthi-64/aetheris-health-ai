"""The seam other modules use to charge a patient for something.

The Laboratory and Pharmacy modules both produce things a patient is billed
for — tests ordered, medicines dispensed. Neither should import Billing or
know how an invoice is structured, so they speak to this protocol instead and
the DI layer hands them Billing's implementation.

Unlike :class:`~app.core.notifications.Notifier`, **a charge sink may raise**.
A notification that fails must not undo the event it describes; a charge that
fails must — "every dispense produces a bill line" is a rule, not a courtesy.
The sink writes in the caller's transaction and never commits, so the charge
and the thing charged for stand or fall together.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime by the dataclass
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

__all__ = ["Charge", "ChargeSink", "NullChargeSink"]


@dataclass(frozen=True, slots=True)
class Charge:
    """One billable line.

    :param description: What the patient is being charged for.
    :param unit_price: Price per unit, in the hospital's currency.
    :param quantity: How many units.
    """

    description: str
    unit_price: Decimal
    quantity: Decimal = Decimal(1)


@runtime_checkable
class ChargeSink(Protocol):
    """Something that can put charges on a patient's bill."""

    async def add_charges(
        self,
        hospital_id: uuid.UUID,
        *,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID | None,
        charges: list[Charge],
        source: str,
        actor_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Add lines to a draft invoice for the patient.

        :param hospital_id: The tenant to scope to.
        :param patient_id: Patient being charged.
        :param appointment_id: The visit the charges belong to, if any.
        :param charges: The lines to add.
        :param source: Which module raised them, for the audit trail.
        :param actor_id: UUID of the acting user.
        :returns: The invoice the lines went onto, or ``None`` if nothing was
            charged.
        """
        ...


class NullChargeSink:
    """A sink that charges nothing — for services built without Billing."""

    async def add_charges(
        self,
        hospital_id: uuid.UUID,
        *,
        patient_id: uuid.UUID,
        appointment_id: uuid.UUID | None,
        charges: list[Charge],
        source: str,
        actor_id: uuid.UUID | None = None,
    ) -> uuid.UUID | None:
        """Accept the charges and do nothing with them."""
        return None
