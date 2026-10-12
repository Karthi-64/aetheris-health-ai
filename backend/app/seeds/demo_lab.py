"""Laboratory demo data — a test catalog to order from.

A section of the one seed mechanism, called by
:func:`app.seeds.demo_data.seed_demo_data`. Split out for the same reason
``demo_billing`` is: length.

**The reference ranges are illustrative.** They are plausible adult and
paediatric values chosen so the demo can show a normal, an abnormal and a
critical result. They are not a validated clinical dataset — the module spec's
open question on which dataset to seed with (§20) is still open — and must be
replaced before any real result is reported against them.

**No orders are seeded.** An order placed in a demo is charged, flagged and
notified live, which is the thing worth showing.

**Idempotency.** A test is looked up by ``(hospital_id, code)`` — the table's
unique constraint — before it is created, so a second run creates nothing and
never overwrites a range an admin has since edited.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

from app.core.logging import get_logger
from app.models.lab import LabResultType
from app.repositories.lab_test_repository import LabTestRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.hospital import Hospital

logger = get_logger(__name__)

__all__ = ["LAB_TESTS", "seed_demo_lab"]

_ADULT = 18


def _by_sex(
    male: tuple[str, str], female: tuple[str, str], **critical: str
) -> list[dict[str, Any]]:
    """Adult ranges that differ by sex."""
    return [
        {"sex": "male", "age_min": _ADULT, "low": male[0], "high": male[1], **critical},
        {"sex": "female", "age_min": _ADULT, "low": female[0], "high": female[1], **critical},
    ]


def _anyone(low: str | None, high: str | None, **critical: str) -> list[dict[str, Any]]:
    """One range for every patient."""
    bounds = {name: value for name, value in (("low", low), ("high", high)) if value is not None}
    return [{"sex": "any", **bounds, **critical}]


#: The demo catalog: ``(code, name, category, unit, type, ranges, hours, price,
#: active)``. One retired test makes the ``is_active`` filter demonstrable, and
#: one text test shows a result with no range.
LAB_TESTS: list[
    tuple[str, str, str, str | None, LabResultType, list[dict[str, Any]], int, str, bool]
] = [
    (
        "HB",
        "Haemoglobin",
        "Haematology",
        "g/dL",
        LabResultType.NUMERIC,
        [
            *_by_sex(("13.0", "17.0"), ("12.0", "15.5"), critical_low="7.0"),
            {"sex": "any", "age_min": 0, "age_max": 17, "low": "11.0", "high": "14.5"},
        ],
        4,
        "250.00",
        True,
    ),
    (
        "WBC",
        "White cell count",
        "Haematology",
        "10^9/L",
        LabResultType.NUMERIC,
        _anyone("4.0", "11.0", critical_low="2.0", critical_high="30.0"),
        4,
        "250.00",
        True,
    ),
    (
        "PLT",
        "Platelet count",
        "Haematology",
        "10^9/L",
        LabResultType.NUMERIC,
        _anyone("150", "450", critical_low="50"),
        4,
        "250.00",
        True,
    ),
    (
        "FBS",
        "Fasting blood sugar",
        "Biochemistry",
        "mg/dL",
        LabResultType.NUMERIC,
        _anyone("70", "100", critical_low="50", critical_high="400"),
        2,
        "150.00",
        True,
    ),
    (
        "HBA1C",
        "HbA1c",
        "Biochemistry",
        "%",
        LabResultType.NUMERIC,
        _anyone(None, "5.6"),
        24,
        "550.00",
        True,
    ),
    (
        "CREAT",
        "Serum creatinine",
        "Biochemistry",
        "mg/dL",
        LabResultType.NUMERIC,
        _by_sex(("0.7", "1.3"), ("0.6", "1.1"), critical_high="5.0"),
        6,
        "300.00",
        True,
    ),
    (
        "K",
        "Serum potassium",
        "Biochemistry",
        "mmol/L",
        LabResultType.NUMERIC,
        _anyone("3.5", "5.1", critical_low="2.5", critical_high="6.5"),
        2,
        "300.00",
        True,
    ),
    (
        "TSH",
        "Thyroid stimulating hormone",
        "Endocrinology",
        "mIU/L",
        LabResultType.NUMERIC,
        _anyone("0.4", "4.0"),
        24,
        "450.00",
        True,
    ),
    (
        "URINE-ME",
        "Urine microscopy",
        "Clinical pathology",
        None,
        LabResultType.TEXT,
        [],
        6,
        "200.00",
        True,
    ),
    (
        "ESR",
        "ESR (retired)",
        "Haematology",
        "mm/hr",
        LabResultType.NUMERIC,
        _anyone(None, "20"),
        4,
        "150.00",
        False,
    ),
]


async def seed_demo_lab(session: AsyncSession, hospital: Hospital) -> int:
    """Create the demo test catalog.

    :param session: An open session inside a transaction.
    :param hospital: The demo hospital.
    :returns: How many tests were created by this run.
    """
    repository = LabTestRepository(session)
    created = 0

    for code, name, category, unit, result_type, ranges, hours, price, is_active in LAB_TESTS:
        if await repository.get_test_by_code(hospital.id, code) is not None:
            continue
        await repository.create_test(
            hospital_id=hospital.id,
            code=code,
            name=name,
            category=category,
            unit=unit,
            result_type=result_type,
            reference_ranges=ranges,
            turnaround_hours=hours,
            price=Decimal(price),
            is_active=is_active,
        )
        created += 1

    logger.info("demo_lab_tests_seeded", total=len(LAB_TESTS), created=created)
    return created
