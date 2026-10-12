"""The seed's report permissions, checked against the module spec.

``docs/modules/10-reports-dashboard.md`` §10 gives each role one dashboard.
The seed silently skips a permission code it does not recognise in a role's
list, so a typo there would not fail ``make seed`` — it would just leave a
role without its dashboard. These tests read the seed's own tables and fail
on exactly that.
"""

from __future__ import annotations

import pytest

from app.seeds.seed import PERMISSION_DEFINITIONS, SYSTEM_ROLES

ADMIN = "report.admin.read"
DOCTOR = "report.doctor.read"
RECEPTION = "report.reception.read"
BILLING = "report.billing.read"
EXPORT = "report.export"

REPORT_CODES = {ADMIN, DOCTOR, RECEPTION, BILLING, EXPORT}

#: Role → the ``report.*`` codes it must hold, no more and no fewer.
EXPECTED: dict[str, set[str]] = {
    "Super Admin": REPORT_CODES,
    # A hospital admin may only assign roles whose permissions they hold
    # themselves, so they hold every code they can hand out.
    "Hospital Admin": REPORT_CODES,
    "Doctor": {DOCTOR},
    "Receptionist": {RECEPTION},
    "Billing Staff": {BILLING, EXPORT},
    "Nurse": set(),
    "Lab Technician": set(),
    "Pharmacist": set(),
    "Inventory Manager": set(),
}


def _report_codes(codes: list[str]) -> set[str]:
    """The codes of the reports module among a list."""
    return {code for code in codes if code.startswith("report.")}


def test_the_catalog_has_exactly_the_five_report_codes() -> None:
    catalog = {code for code, module, _ in PERMISSION_DEFINITIONS if module == "reports"}

    assert catalog == REPORT_CODES
    assert _report_codes([code for code, _, _ in PERMISSION_DEFINITIONS]) == REPORT_CODES


def test_the_old_single_read_code_and_the_ai_code_are_not_seeded() -> None:
    catalog = {code for code, _, _ in PERMISSION_DEFINITIONS}

    assert "report.read" not in catalog
    assert "report.ai_summary" not in catalog
    for _, _, codes in SYSTEM_ROLES:
        assert "report.read" not in codes


def test_each_report_code_is_defined_once_with_a_description() -> None:
    rows = [(code, text) for code, module, text in PERMISSION_DEFINITIONS if module == "reports"]

    assert len(rows) == len(REPORT_CODES)
    assert all(text.strip() for _, text in rows)


def test_every_seeded_role_is_covered() -> None:
    assert {name for name, _, _ in SYSTEM_ROLES} == set(EXPECTED)


@pytest.mark.parametrize("role", sorted(EXPECTED))
def test_a_role_holds_exactly_its_report_codes(role: str) -> None:
    codes = next(codes for name, _, codes in SYSTEM_ROLES if name == role)

    assert _report_codes(codes) == EXPECTED[role]
    assert len(codes) == len(set(codes)), f"{role} lists a permission twice"


def test_every_code_a_role_lists_exists_in_the_catalog() -> None:
    """A code missing from the catalog is skipped by the seed without a word."""
    catalog = {code for code, _, _ in PERMISSION_DEFINITIONS}

    for name, _, codes in SYSTEM_ROLES:
        assert set(codes) <= catalog, f"{name} lists unseeded codes: {sorted(set(codes) - catalog)}"


def test_the_hospital_admin_holds_every_report_code_any_role_holds() -> None:
    admin = next(set(codes) for name, _, codes in SYSTEM_ROLES if name == "Hospital Admin")

    for name, _, codes in SYSTEM_ROLES:
        if name != "Super Admin":
            assert _report_codes(codes) <= admin, name
