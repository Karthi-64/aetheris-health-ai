"""Pieces the three Pharmacy services share.

The module is one bounded context split across services by length — catalog
and stock, prescriptions and dispensing, vendors and purchase orders — and
these are the few things all of them need.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.exceptions import NotFoundError, ValidationError
from app.core.logging import get_logger

if TYPE_CHECKING:
    from app.repositories.hospital_repository import HospitalRepository

logger = get_logger(__name__)

__all__ = ["MedicineNotFoundError", "audit_value", "field_error", "hospital_today"]


class MedicineNotFoundError(NotFoundError):
    """Raised when a medicine is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, medicine_id: uuid.UUID) -> None:
        super().__init__(message="Medicine not found.", detail={"medicine_id": str(medicine_id)})


def field_error(field: str, message: str) -> ValidationError:
    """Build a 422 naming one offending field, in the standard error shape."""
    return ValidationError(
        message=message, detail={"errors": [{"field": field, "message": message}]}
    )


def audit_value(value: Any) -> Any:
    """Render a column value for an audit ``changes`` entry.

    The durable audit store keeps ``changes`` as JSON, so anything that is not
    already JSON-native — a ``Decimal``, a date — goes in as its string form.
    """
    return value if isinstance(value, bool | str | int) or value is None else str(value)


async def hospital_today(hospitals: HospitalRepository, hospital_id: uuid.UUID) -> date:
    """Return today's date where the hospital is.

    Expiry is a calendar date, and "a batch expiring today is still
    dispensable" (module spec §14) means today on the pharmacy's wall, not in
    UTC — a hospital in India is a day ahead of UTC for part of every night.
    An unknown or malformed timezone falls back to UTC and is logged.

    :param hospitals: Hospital lookups.
    :param hospital_id: The tenant.
    :returns: The current local date.
    """
    hospital = await hospitals.get_by_id(hospital_id)
    zone = UTC
    if hospital is not None:
        try:
            return datetime.now(ZoneInfo(hospital.timezone)).date()
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning(
                "pharmacy.hospital_timezone_invalid",
                hospital_id=str(hospital_id),
                timezone=hospital.timezone,
            )
    return datetime.now(zone).date()
