"""API tests for the Reports and Dashboards endpoints.

Real app, real service, real repository, real database — only the HTTP
transport is in-process (``docs/11-TESTING_STRATEGY.md`` §2.3).

**The clock is pinned.** Every test runs at 14:02:11 on Tuesday 6 October 2026
in Kolkata (08:32:11 UTC), by patching ``app.services.report_service.utc_now``,
and every fixture row carries an explicit timestamp. "Today" is therefore the
same local day whenever the suite runs — including between midnight and 05:30
in India, when the UTC date is a day behind.

Figures are checked against the hand-computed fixtures of
``app/tests/report_helpers.py``.
"""

from __future__ import annotations

import csv
import io
import re
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.auth import get_current_user
from app.api.dependencies.db import get_db_session
from app.api.dependencies.services import get_audit_sink
from app.main import create_app
from app.models.appointment import AppointmentStatus
from app.models.user import User, UserStatus
from app.seeds.seed import PERMISSION_DEFINITIONS, SYSTEM_ROLES
from app.services import report_service
from app.tests.billing_helpers import auth_headers, insert_user_with_permissions
from app.tests.conftest import RecordingAuditSink
from app.tests.report_helpers import (
    REGISTERED_LONG_AGO,
    Clinic,
    insert_named_doctor,
    insert_report_appointment,
    insert_report_patient,
    seed_billing,
    seed_clinic,
    seed_other_billing,
    seed_other_clinic,
    seed_reception_desk,
    seed_registrations,
    utc,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from fastapi import FastAPI
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

#: 08:32:11 UTC is 14:02:11 in Asia/Kolkata on Tuesday 6 Oct 2026.
NOW = datetime(2026, 10, 6, 8, 32, 11, 482913, tzinfo=UTC)

DASH = "/api/v1/dashboards"
REPORTS = "/api/v1/reports"

ADMIN = "report.admin.read"
DOCTOR = "report.doctor.read"
RECEPTION = "report.reception.read"
BILLING = "report.billing.read"
EXPORT = "report.export"
ALL_REPORT_CODES = [ADMIN, DOCTOR, RECEPTION, BILLING, EXPORT]

#: (path, codes that open it, whether the caller needs a doctor profile).
ENDPOINTS: list[tuple[str, tuple[str, ...], bool]] = [
    (f"{DASH}/admin", (ADMIN,), False),
    (f"{DASH}/doctor", (DOCTOR,), True),
    (f"{DASH}/reception", (RECEPTION,), False),
    (f"{DASH}/billing", (BILLING,), False),
    (f"{REPORTS}/patients", (ADMIN,), False),
    (f"{REPORTS}/appointments", (ADMIN,), False),
    (f"{REPORTS}/revenue", (BILLING,), False),
    (f"{REPORTS}/outstanding", (BILLING,), False),
    (f"{REPORTS}/revenue/export", (EXPORT, BILLING), False),
]
PATHS = [path for path, _, _ in ENDPOINTS]

EXPORTS = [
    f"{REPORTS}/{name}/export" for name in ("patients", "appointments", "revenue", "outstanding")
]
EVERY_ROUTE = [*PATHS[:8], *EXPORTS]

#: Every seeded permission that is not a report permission.
OTHER_MODULES = [code for code, _, _ in PERMISSION_DEFINITIONS if not code.startswith("report.")]

MONEY = re.compile(r"^-?\d+\.\d{2}$")
GENERATED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

COUNT_KEYS = {"total", "booked", "checked_in", "in_progress", "completed", "cancelled", "no_show"}
REVENUE_KEYS = {
    "invoice_count",
    "invoiced_amount",
    "payment_count",
    "collected_amount",
    "refund_count",
    "refunded_amount",
    "net_collected_amount",
}
BUCKET_KEYS = {"bucket_start", "bucket_end", "partial"}
META_KEYS = {"hospital_name", "timezone", "currency", "today", "generated_at"}
OUTSTANDING_KEYS = {"invoice_count", "outstanding_amount", "issued_count", "partially_paid_count"}


@pytest.fixture(autouse=True)
def pinned_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every request at :data:`NOW`."""
    monkeypatch.setattr(report_service, "utc_now", lambda: NOW)


@pytest.fixture
def audit() -> RecordingAuditSink:
    """The sink the app records to for the duration of a test."""
    return RecordingAuditSink()


@pytest_asyncio.fixture
async def application(db_session: AsyncSession, audit: RecordingAuditSink) -> FastAPI:
    """The app, sharing the test's rolled-back session and audit sink."""
    app: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db_session] = _session_override
    app.dependency_overrides[get_audit_sink] = lambda: audit
    return app


@pytest_asyncio.fixture
async def api(application: FastAPI) -> AsyncGenerator[AsyncClient]:
    """An HTTP client against the app."""
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest.fixture
def login(
    db_session: AsyncSession, hospital_id: uuid.UUID
) -> Callable[..., Awaitable[dict[str, str]]]:
    """Create a user holding the given codes and return their auth header.

    ``doctor=True`` also gives the user an active doctor profile.
    ``hospital`` puts the user in another hospital.
    """

    async def _login(
        *codes: str, doctor: bool = False, hospital: uuid.UUID | None = None
    ) -> dict[str, str]:
        tenant = hospital or hospital_id
        user = await insert_user_with_permissions(db_session, tenant, list(codes))
        if doctor:
            await insert_named_doctor(
                db_session, tenant, first_name="-", last_name="-", user_id=user.id
            )
        return auth_headers(user.id, tenant)

    return _login


def data(response: Response) -> dict[str, Any]:
    """Return the ``data`` of a successful envelope."""
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    payload: dict[str, Any] = body["data"]
    return payload


def rule_error(response: Response) -> tuple[int, str, str]:
    """Return ``(status, message, field)`` of a rule error raised by this module."""
    body = response.json()
    assert body["success"] is False
    first = body["errors"]["errors"][0]
    assert first["message"] == body["message"]
    return response.status_code, body["message"], first["field"]


def csv_rows(response: Response) -> list[list[str]]:
    """Parse a CSV download, checking its byte-order mark."""
    assert response.status_code == 200, response.text
    assert response.content.startswith(b"\xef\xbb\xbf")
    return list(csv.reader(io.StringIO(response.content.decode("utf-8-sig"))))


def assert_money(value: Any) -> None:
    """Assert a JSON value is a decimal string with two places."""
    assert isinstance(value, str), f"{value!r} is not a string"
    assert MONEY.match(value), f"{value!r} is not a money string"


def walk(node: Any, path: str = "") -> list[tuple[str, Any]]:
    """Flatten a JSON document to ``(dotted path, leaf value)`` pairs."""
    if isinstance(node, dict):
        return [pair for key, value in node.items() for pair in walk(value, f"{path}.{key}")]
    if isinstance(node, list):
        return [pair for item in node for pair in walk(item, f"{path}[]")]
    return [(path, node)]


# ── Authentication and permissions ───────────────────────────────────────────


class TestAccess:
    @pytest.mark.parametrize("path", EVERY_ROUTE)
    async def test_no_token_is_401(self, api: AsyncClient, path: str) -> None:
        response = await api.get(path)

        assert response.status_code == 401
        assert response.json()["error_code"] == "AUTHENTICATION_REQUIRED"

    @pytest.mark.parametrize("path", EVERY_ROUTE)
    async def test_every_other_modules_permissions_together_are_403(
        self, api: AsyncClient, login: Any, path: str
    ) -> None:
        response = await api.get(path, headers=await login(*OTHER_MODULES, doctor=True))

        assert response.status_code == 403
        assert response.json()["error_code"] == "PERMISSION_DENIED"

    @pytest.mark.parametrize(("path", "codes", "needs_doctor"), ENDPOINTS)
    async def test_its_own_codes_are_200(
        self, api: AsyncClient, login: Any, path: str, codes: tuple[str, ...], needs_doctor: bool
    ) -> None:
        response = await api.get(path, headers=await login(*codes, doctor=needs_doctor))

        assert response.status_code == 200, response.text

    @pytest.mark.parametrize(
        ("path", "message"),
        [
            (f"{DASH}/admin", "Permission denied. Required: report.admin.read."),
            (f"{DASH}/doctor", "Permission denied. Required: report.doctor.read."),
            (f"{DASH}/reception", "Permission denied. Required: report.reception.read."),
            (f"{DASH}/billing", "Permission denied. Required: report.billing.read."),
            (f"{REPORTS}/patients", "Permission denied. Required: report.admin.read."),
            (f"{REPORTS}/appointments", "Permission denied. Required: report.admin.read."),
            (
                f"{REPORTS}/revenue",
                "Permission denied. Required one of: report.admin.read, report.billing.read.",
            ),
            (
                f"{REPORTS}/outstanding",
                "Permission denied. Required one of: report.admin.read, report.billing.read.",
            ),
            (f"{REPORTS}/revenue/export", "Permission denied. Required: report.export."),
        ],
    )
    async def test_the_403_names_what_is_required(
        self, api: AsyncClient, login: Any, path: str, message: str
    ) -> None:
        response = await api.get(path, headers=await login())

        assert response.status_code == 403
        assert response.json()["message"] == message

    @pytest.mark.parametrize("report", ["revenue", "outstanding"])
    @pytest.mark.parametrize("code", [BILLING, ADMIN])
    async def test_financial_reports_open_with_either_code(
        self, api: AsyncClient, login: Any, report: str, code: str
    ) -> None:
        response = await api.get(f"{REPORTS}/{report}", headers=await login(code))

        assert response.status_code == 200

    @pytest.mark.parametrize("report", ["patients", "appointments"])
    async def test_hospital_wide_reports_are_closed_to_the_billing_code(
        self, api: AsyncClient, login: Any, report: str
    ) -> None:
        headers = await login(BILLING, EXPORT, DOCTOR, RECEPTION)

        report_response = await api.get(f"{REPORTS}/{report}", headers=headers)
        export_response = await api.get(f"{REPORTS}/{report}/export", headers=headers)

        assert (report_response.status_code, export_response.status_code) == (403, 403)
        assert export_response.json()["message"] == (
            "Permission denied. Required: report.admin.read."
        )

    @pytest.mark.parametrize(
        ("dashboard", "own"),
        [("admin", ADMIN), ("doctor", DOCTOR), ("reception", RECEPTION), ("billing", BILLING)],
    )
    async def test_each_dashboard_is_closed_to_the_other_three_dashboard_codes(
        self, api: AsyncClient, login: Any, dashboard: str, own: str
    ) -> None:
        others = [code for code in (ADMIN, DOCTOR, RECEPTION, BILLING) if code != own]

        response = await api.get(
            f"{DASH}/{dashboard}", headers=await login(*others, EXPORT, doctor=True)
        )

        assert response.status_code == 403
        assert response.json()["message"] == f"Permission denied. Required: {own}."


def _role_codes(role: str) -> list[str]:
    """The permission codes the seed gives a role."""
    return next(list(codes) for name, _, codes in SYSTEM_ROLES if name == role)


_BILLING_ROUTES = {
    f"{DASH}/billing",
    f"{REPORTS}/revenue",
    f"{REPORTS}/outstanding",
    f"{REPORTS}/revenue/export",
    f"{REPORTS}/outstanding/export",
}

#: Role → the routes that answer 200. Everything else in EVERY_ROUTE is 403.
ROLE_ACCESS: dict[str, set[str]] = {
    "Super Admin": set(EVERY_ROUTE),
    "Hospital Admin": set(EVERY_ROUTE),
    "Doctor": {f"{DASH}/doctor"},
    "Receptionist": {f"{DASH}/reception"},
    "Billing Staff": _BILLING_ROUTES,
    "Nurse": set(),
    "Lab Technician": set(),
    "Pharmacist": set(),
    "Inventory Manager": set(),
}


class TestSeededRoles:
    def test_the_matrix_covers_every_seeded_role(self) -> None:
        assert set(ROLE_ACCESS) == {name for name, _, _ in SYSTEM_ROLES}

    @pytest.mark.parametrize("role", sorted(ROLE_ACCESS))
    async def test_a_role_reaches_exactly_its_own_routes(
        self, api: AsyncClient, login: Any, role: str
    ) -> None:
        """With the codes the seed really grants, a doctor profile attached."""
        headers = await login(*_role_codes(role), doctor=True)

        statuses = {
            path: (await api.get(path, headers=headers)).status_code for path in EVERY_ROUTE
        }

        assert {path for path, status in statuses.items() if status == 200} == ROLE_ACCESS[role]
        assert {path for path, status in statuses.items() if status == 403} == (
            set(EVERY_ROUTE) - ROLE_ACCESS[role]
        )

    async def test_a_hospital_admin_without_a_doctor_profile_has_no_doctor_dashboard(
        self, api: AsyncClient, login: Any
    ) -> None:
        headers = await login(*_role_codes("Hospital Admin"))

        response = await api.get(f"{DASH}/doctor", headers=headers)

        assert response.status_code == 404
        for path in EVERY_ROUTE:
            if path != f"{DASH}/doctor":
                assert (await api.get(path, headers=headers)).status_code == 200, path


# ── No hospital ──────────────────────────────────────────────────────────────


class TestNoHospital:
    """A caller scoped to no hospital passes every permission gate, then stops.

    ``users.hospital_id`` is ``NOT NULL`` in the schema, so no such row can be
    inserted; the authenticated user is supplied by overriding the dependency.
    """

    @pytest.fixture
    def platform_user(self, application: FastAPI) -> None:
        user = User(
            id=uuid.uuid4(),
            hospital_id=None,
            email="platform@aetheris.test",
            password_hash="test-placeholder-not-a-hash",
            first_name="Platform",
            last_name="Account",
            status=UserStatus.ACTIVE,
        )
        application.dependency_overrides[get_current_user] = lambda: user

    @pytest.mark.parametrize("path", EVERY_ROUTE)
    async def test_every_route_is_a_400(
        self, api: AsyncClient, platform_user: None, audit: RecordingAuditSink, path: str
    ) -> None:
        response = await api.get(path)

        assert response.status_code == 400
        body = response.json()
        assert body["error_code"] == "BUSINESS_RULE_VIOLATION"
        assert body["message"] == (
            "This account is not scoped to a hospital, so reports cannot be read."
        )
        assert audit.events == []

    async def test_a_malformed_date_is_reported_first(
        self, api: AsyncClient, platform_user: None
    ) -> None:
        malformed = await api.get(f"{REPORTS}/revenue", params={"from": "garbage"})
        well_formed = await api.get(f"{REPORTS}/revenue", params={"from": "2026-10-01"})

        assert (malformed.status_code, well_formed.status_code) == (422, 400)

    async def test_the_tenant_check_comes_before_the_unknown_report_check(
        self, api: AsyncClient, platform_user: None
    ) -> None:
        response = await api.get(f"{REPORTS}/bogus/export")

        assert response.status_code == 400


# ── Order of checks and validation ───────────────────────────────────────────


class TestOrderOfChecks:
    async def test_a_type_error_wins_over_an_unknown_parameter(
        self, api: AsyncClient, login: Any
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue?foo=1&from=garbage", headers=await login(BILLING)
        )

        body = response.json()
        assert response.status_code == 422
        assert body["message"] == "Validation failed."
        assert body["error_code"] == "VALIDATION_ERROR"
        assert [error["field"] for error in body["errors"]] == ["query.from"]

    async def test_with_a_well_formed_date_the_unknown_parameter_is_named(
        self, api: AsyncClient, login: Any
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue?foo=1&from=2026-10-01", headers=await login(BILLING)
        )

        assert rule_error(response) == (422, "Unknown query parameter: `foo`.", "foo")

    async def test_the_first_unknown_parameter_in_request_order_is_named(
        self, api: AsyncClient, login: Any
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue?zeta=1&from=2026-10-01&alpha=2", headers=await login(BILLING)
        )

        assert rule_error(response)[1] == "Unknown query parameter: `zeta`."

    async def test_an_unknown_report_is_422_until_its_dates_parse_then_404(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink
    ) -> None:
        headers = await login(EXPORT, ADMIN)

        malformed = await api.get(f"{REPORTS}/bogus/export?from=xyz", headers=headers)
        unknown = await api.get(f"{REPORTS}/bogus/export", headers=headers)
        inventory = await api.get(f"{REPORTS}/inventory/export", headers=headers)

        assert malformed.status_code == 422
        assert malformed.json()["message"] == "Validation failed."
        assert unknown.status_code == 404
        assert unknown.json()["error_code"] == "RESOURCE_NOT_FOUND"
        assert unknown.json()["message"] == "Unknown report: `bogus`."
        assert inventory.json()["message"] == "Unknown report: `inventory`."
        assert audit.events == []

    async def test_an_unknown_parameter_is_reported_before_a_refused_format(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue/export?format=pdf&foo=1", headers=await login(EXPORT, BILLING)
        )

        assert rule_error(response) == (422, "Unknown query parameter: `foo`.", "foo")
        assert audit.events == []

    async def test_without_the_permission_a_malformed_date_is_still_403(
        self, api: AsyncClient, login: Any
    ) -> None:
        headers = await login()

        for path in (f"{REPORTS}/revenue", f"{REPORTS}/patients", f"{REPORTS}/revenue/export"):
            response = await api.get(path, params={"from": "garbage"}, headers=headers)
            assert response.status_code == 403, path

    async def test_the_report_read_check_comes_before_the_unknown_parameter_check(
        self, api: AsyncClient, login: Any
    ) -> None:
        response = await api.get(
            f"{REPORTS}/patients/export?foo=1", headers=await login(EXPORT, BILLING)
        )

        assert response.status_code == 403


class TestValidation:
    @pytest.mark.parametrize(
        ("query", "field"),
        [
            ("from=06-10-2026", "query.from"),
            ("to=2026-13-01", "query.to"),
            ("doctor_id=not-a-uuid", "query.doctor_id"),
            ("department_id=123", "query.department_id"),
        ],
    )
    async def test_a_malformed_value_is_a_type_error_on_that_parameter(
        self, api: AsyncClient, login: Any, query: str, field: str
    ) -> None:
        response = await api.get(f"{REPORTS}/appointments?{query}", headers=await login(ADMIN))

        body = response.json()
        assert response.status_code == 422
        assert body["message"] == "Validation failed."
        assert [error["field"] for error in body["errors"]] == [field]

    @pytest.mark.parametrize("report", ["patients", "appointments", "revenue"])
    @pytest.mark.parametrize(
        ("query", "message", "field"),
        [
            ("from=2026-10-06&to=2026-10-05", "`to` must not be before `from`.", "to"),
            ("from=2026-01-01&to=2027-01-01", "Date range must not exceed 12 months.", "to"),
            (
                "granularity=hour",
                "`granularity` must be one of: day, week, month.",
                "granularity",
            ),
            ("granularity=", "`granularity` must be one of: day, week, month.", "granularity"),
        ],
    )
    async def test_period_rules(
        self, api: AsyncClient, login: Any, report: str, query: str, message: str, field: str
    ) -> None:
        headers = await login(ADMIN, EXPORT)

        report_response = await api.get(f"{REPORTS}/{report}?{query}", headers=headers)
        export_response = await api.get(f"{REPORTS}/{report}/export?{query}", headers=headers)

        assert rule_error(report_response) == (422, message, field)
        assert rule_error(export_response) == (422, message, field)

    @pytest.mark.parametrize("report", ["patients", "appointments", "revenue"])
    @pytest.mark.parametrize(
        ("query", "field"),
        [
            ("to=9999-12-31", "to"),
            ("from=9999-12-01&to=9999-12-31", "from"),
            ("from=0001-01-01&to=0001-01-02", "from"),
            ("to=0001-01-05", "to"),
            ("from=3000-01-01", "from"),
            ("to=1899-12-31", "to"),
        ],
    )
    async def test_a_date_at_the_ends_of_the_calendar_is_a_422_not_a_500(
        self,
        api: AsyncClient,
        login: Any,
        audit: RecordingAuditSink,
        report: str,
        query: str,
        field: str,
    ) -> None:
        headers = await login(ADMIN, EXPORT)
        message = f"`{field}` must be between 1900-01-01 and 2999-12-31."

        report_response = await api.get(f"{REPORTS}/{report}?{query}", headers=headers)
        export_response = await api.get(f"{REPORTS}/{report}/export?{query}", headers=headers)

        assert rule_error(report_response) == (422, message, field)
        assert rule_error(export_response) == (422, message, field)
        assert audit.events == []

    @pytest.mark.parametrize("report", ["patients", "appointments", "revenue"])
    @pytest.mark.parametrize(
        "query",
        [
            "from=1900-01-01&to=1900-12-31&granularity=week",
            "from=2999-01-01&to=2999-12-31&granularity=month",
            "to=1900-01-01",
            "from=2999-12-31&to=2999-12-31",
        ],
    )
    async def test_the_first_and_last_supported_dates_answer_200(
        self, api: AsyncClient, login: Any, report: str, query: str
    ) -> None:
        headers = await login(ADMIN, EXPORT)

        report_response = await api.get(f"{REPORTS}/{report}?{query}", headers=headers)
        export_response = await api.get(f"{REPORTS}/{report}/export?{query}", headers=headers)

        assert data(report_response)["buckets"]
        assert export_response.status_code == 200, export_response.text

    @pytest.mark.parametrize(
        ("path", "field"),
        [
            (f"{REPORTS}/revenue?from=2026-10-01&from=2026-10-03", "from"),
            (f"{REPORTS}/revenue?to=2026-10-06&granularity=day&to=2026-10-06", "to"),
            (f"{REPORTS}/patients?granularity=day&granularity=week", "granularity"),
            (f"{REPORTS}/revenue/export?format=csv&format=csv", "format"),
            (f"{REPORTS}/appointments/export?granularity=day&granularity=week", "granularity"),
        ],
    )
    async def test_a_parameter_given_twice_is_refused(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink, path: str, field: str
    ) -> None:
        response = await api.get(path, headers=await login(ADMIN, EXPORT))

        assert rule_error(response) == (
            422,
            f"Query parameter `{field}` must be given only once.",
            field,
        )
        assert audit.events == []

    async def test_an_unknown_parameter_is_named_before_a_repeated_one_in_request_order(
        self, api: AsyncClient, login: Any
    ) -> None:
        headers = await login(ADMIN)

        unknown_first = await api.get(
            f"{REPORTS}/revenue?foo=1&granularity=day&granularity=day", headers=headers
        )
        repeat_first = await api.get(
            f"{REPORTS}/revenue?granularity=day&granularity=day&foo=1", headers=headers
        )

        assert rule_error(unknown_first)[1] == "Unknown query parameter: `foo`."
        assert (
            rule_error(repeat_first)[1] == "Query parameter `granularity` must be given only once."
        )

    async def test_twelve_months_to_the_day_is_accepted(self, api: AsyncClient, login: Any) -> None:
        response = await api.get(
            f"{REPORTS}/revenue?from=2026-01-01&to=2026-12-31&granularity=month",
            headers=await login(BILLING),
        )

        assert len(data(response)["buckets"]) == 12

    @pytest.mark.parametrize(
        "path", [f"{DASH}/{name}" for name in ("admin", "doctor", "reception", "billing")]
    )
    async def test_dashboards_take_no_parameters(
        self, api: AsyncClient, login: Any, path: str
    ) -> None:
        headers = await login(ADMIN, DOCTOR, RECEPTION, BILLING, doctor=True)

        for query, key in (
            ("foo=1", "foo"),
            ("from=2026-10-01", "from"),
            ("granularity=day", "granularity"),
        ):
            response = await api.get(f"{path}?{query}", headers=headers)
            assert rule_error(response) == (422, f"Unknown query parameter: `{key}`.", key)

    @pytest.mark.parametrize("key", ["from", "to", "granularity", "foo"])
    async def test_the_outstanding_report_takes_no_parameters(
        self, api: AsyncClient, login: Any, key: str
    ) -> None:
        headers = await login(BILLING, EXPORT)

        report = await api.get(f"{REPORTS}/outstanding?{key}=2026-10-01", headers=headers)
        export = await api.get(f"{REPORTS}/outstanding/export?{key}=2026-10-01", headers=headers)

        assert rule_error(report) == (422, f"Unknown query parameter: `{key}`.", key)
        assert rule_error(export) == (422, f"Unknown query parameter: `{key}`.", key)

    @pytest.mark.parametrize("report", ["patients", "revenue"])
    async def test_doctor_filters_belong_to_the_appointments_report_only(
        self, api: AsyncClient, login: Any, report: str
    ) -> None:
        response = await api.get(
            f"{REPORTS}/{report}?doctor_id={uuid.uuid4()}", headers=await login(ADMIN)
        )

        assert rule_error(response) == (422, "Unknown query parameter: `doctor_id`.", "doctor_id")

    async def test_a_doctor_or_department_of_another_hospital_is_not_found(
        self,
        api: AsyncClient,
        login: Any,
        db_session: AsyncSession,
        other_hospital_id: uuid.UUID,
    ) -> None:
        theirs = await seed_other_clinic(db_session, other_hospital_id)
        headers = await login(ADMIN)

        doctor = await api.get(f"{REPORTS}/appointments?doctor_id={theirs.priya}", headers=headers)
        department = await api.get(
            f"{REPORTS}/appointments?department_id={theirs.cardiology}", headers=headers
        )
        nobody = await api.get(f"{REPORTS}/appointments?doctor_id={uuid.uuid4()}", headers=headers)

        assert rule_error(doctor) == (422, "Doctor not found.", "doctor_id")
        assert rule_error(department) == (422, "Department not found.", "department_id")
        assert rule_error(nobody) == (422, "Doctor not found.", "doctor_id")


# ── Figures ──────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def world(
    db_session: AsyncSession, hospital_id: uuid.UUID, other_hospital_id: uuid.UUID
) -> Clinic:
    """Every hand-computed fixture in the hospital under test, and another hospital's data."""
    await seed_billing(db_session, hospital_id)
    clinic = await seed_clinic(db_session, hospital_id)
    await seed_registrations(db_session, hospital_id)
    await seed_other_billing(db_session, other_hospital_id)
    await seed_other_clinic(db_session, other_hospital_id)
    return clinic


META = {
    "timezone": "Asia/Kolkata",
    "currency": "INR",
    "today": "2026-10-06",
    "generated_at": "2026-10-06T08:32:11Z",
}

#: 1-6 Oct, from the billing fixture.
OCTOBER = {
    "invoice_count": 6,
    "invoiced_amount": "4350.10",
    "payment_count": 7,
    "collected_amount": "2300.10",
    "refund_count": 2,
    "refunded_amount": "950.00",
    "net_collected_amount": "1350.10",
}
#: Monday 5 Oct to Tuesday 6 Oct: I3 750.00 + I4 1100.00 + I9 0.10 + I7 600.00
#: + I8 800.00 billed; 300.10 + 1400.00 collected.
THIS_WEEK = {
    "invoice_count": 5,
    "invoiced_amount": "3250.10",
    "payment_count": 6,
    "collected_amount": "1700.10",
    "refund_count": 2,
    "refunded_amount": "950.00",
    "net_collected_amount": "750.10",
}
TODAY = {
    "invoice_count": 2,
    "invoiced_amount": "1400.00",
    "payment_count": 2,
    "collected_amount": "1400.00",
    "refund_count": 2,
    "refunded_amount": "950.00",
    "net_collected_amount": "450.00",
}
UNPAID = {
    "invoice_count": 2,
    "outstanding_amount": "1550.00",
    "issued_count": 1,
    "partially_paid_count": 1,
}


class TestRevenueReport:
    async def test_summary_methods_and_buckets_match_the_hand_computed_fixture(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue?from=2026-10-01&to=2026-10-06", headers=await login(BILLING)
        )

        assert response.json()["message"] == "Revenue report generated."
        report = data(response)
        assert set(report) == {"meta", "filters", "summary", "by_method", "buckets"}
        assert {key: report["meta"][key] for key in META} == META
        assert report["meta"]["hospital_name"].startswith("Test Hospital")
        assert report["filters"] == {"from": "2026-10-01", "to": "2026-10-06", "granularity": "day"}
        assert report["summary"] == OCTOBER
        assert report["by_method"] == [
            {"method": "cash", "payment_count": 1, "collected_amount": "0.03",
             "refund_count": 0, "refunded_amount": "0.00", "net_collected_amount": "0.03"},
            {"method": "card", "payment_count": 2, "collected_amount": "1100.00",
             "refund_count": 1, "refunded_amount": "350.00", "net_collected_amount": "750.00"},
            {"method": "upi", "payment_count": 3, "collected_amount": "1200.03",
             "refund_count": 1, "refunded_amount": "600.00", "net_collected_amount": "600.03"},
            {"method": "bank_transfer", "payment_count": 1, "collected_amount": "0.04",
             "refund_count": 0, "refunded_amount": "0.00", "net_collected_amount": "0.04"},
            {"method": "insurance", "payment_count": 0, "collected_amount": "0.00",
             "refund_count": 0, "refunded_amount": "0.00", "net_collected_amount": "0.00"},
        ]  # fmt: skip
        assert [
            (b["bucket_start"], b["invoiced_amount"], b["collected_amount"], b["refunded_amount"],
             b["net_collected_amount"])
            for b in report["buckets"]
        ] == [
            ("2026-10-01", "1100.00", "600.00", "0.00", "600.00"),
            ("2026-10-02", "0.00", "0.00", "0.00", "0.00"),
            ("2026-10-03", "0.00", "0.00", "0.00", "0.00"),
            ("2026-10-04", "0.00", "0.00", "0.00", "0.00"),
            ("2026-10-05", "1850.10", "300.10", "0.00", "300.10"),
            ("2026-10-06", "1400.00", "1400.00", "950.00", "450.00"),
        ]  # fmt: skip

    @pytest.mark.parametrize("granularity", ["day", "week", "month"])
    async def test_buckets_and_methods_each_add_up_to_the_summary(
        self, api: AsyncClient, login: Any, world: Clinic, granularity: str
    ) -> None:
        report = data(
            await api.get(
                f"{REPORTS}/revenue?from=2026-09-30&to=2026-10-06&granularity={granularity}",
                headers=await login(ADMIN),
            )
        )
        summary = report["summary"]

        for key in REVENUE_KEYS:
            total = sum(Decimal(str(bucket[key])) for bucket in report["buckets"])
            assert total == Decimal(str(summary[key])), key
        for key in ("payment_count", "collected_amount", "refund_count", "refunded_amount",
                    "net_collected_amount"):  # fmt: skip
            total = sum(Decimal(str(row[key])) for row in report["by_method"])
            assert total == Decimal(str(summary[key])), key
        assert summary["collected_amount"] == "2800.10"

    async def test_the_day_before_holds_only_the_payment_made_before_local_midnight(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        report = data(
            await api.get(
                f"{REPORTS}/revenue?from=2026-09-30&to=2026-09-30", headers=await login(BILLING)
            )
        )

        assert report["summary"] == {
            "invoice_count": 0,
            "invoiced_amount": "0.00",
            "payment_count": 1,
            "collected_amount": "500.00",
            "refund_count": 0,
            "refunded_amount": "0.00",
            "net_collected_amount": "500.00",
        }

    async def test_week_and_month_buckets_say_when_they_are_partial(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        headers = await login(BILLING)
        query = "from=2026-09-30&to=2026-10-06"

        weeks = data(await api.get(f"{REPORTS}/revenue?{query}&granularity=week", headers=headers))
        months = data(
            await api.get(f"{REPORTS}/revenue?{query}&granularity=month", headers=headers)
        )

        assert [
            (b["bucket_start"], b["bucket_end"], b["partial"], b["collected_amount"])
            for b in weeks["buckets"]
        ] == [
            ("2026-09-28", "2026-10-04", True, "1100.00"),
            ("2026-10-05", "2026-10-11", True, "1700.10"),
        ]
        assert [
            (b["bucket_start"], b["bucket_end"], b["partial"], b["collected_amount"])
            for b in months["buckets"]
        ] == [
            ("2026-09-01", "2026-09-30", True, "500.00"),
            ("2026-10-01", "2026-10-31", True, "2300.10"),
        ]

    async def test_defaults_are_the_thirty_days_ending_today_by_day(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        report = data(await api.get(f"{REPORTS}/revenue", headers=await login(BILLING)))

        assert report["filters"] == {"from": "2026-09-07", "to": "2026-10-06", "granularity": "day"}
        assert len(report["buckets"]) == 30
        assert report["summary"]["collected_amount"] == "2800.10"


class TestOutstandingReport:
    async def test_summary_ageing_and_invoices(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        response = await api.get(f"{REPORTS}/outstanding", headers=await login(BILLING))

        assert response.json()["message"] == "Outstanding report generated."
        report = data(response)
        assert set(report) == {
            "meta",
            "filters",
            "summary",
            "ageing",
            "invoices",
            "invoices_total",
            "invoices_truncated",
        }
        assert report["filters"] == {"as_of_date": "2026-10-06"}
        assert report["summary"] == UNPAID
        assert report["ageing"] == [
            {"bucket": "0_30", "min_days": 0, "max_days": 30, "invoice_count": 2,
             "outstanding_amount": "1550.00"},
            {"bucket": "31_60", "min_days": 31, "max_days": 60, "invoice_count": 0,
             "outstanding_amount": "0.00"},
            {"bucket": "61_90", "min_days": 61, "max_days": 90, "invoice_count": 0,
             "outstanding_amount": "0.00"},
            {"bucket": "over_90", "min_days": 91, "max_days": None, "invoice_count": 0,
             "outstanding_amount": "0.00"},
        ]  # fmt: skip
        assert (report["invoices_total"], report["invoices_truncated"]) == (2, False)
        first, second = report["invoices"]
        assert set(first) == {
            "invoice_id",
            "invoice_number",
            "issued_at",
            "issued_date",
            "age_days",
            "patient_id",
            "patient_name",
            "patient_mrn",
            "status",
            "total",
            "amount_paid",
            "balance_due",
        }
        assert (first["status"], first["total"], first["amount_paid"], first["balance_due"]) == (
            "partially_paid",
            "750.00",
            "300.00",
            "450.00",
        )
        assert (first["issued_at"], first["issued_date"], first["age_days"]) == (
            "2026-10-05T05:10:00Z",
            "2026-10-05",
            1,
        )
        assert (second["status"], second["balance_due"]) == ("issued", "1100.00")
        assert first["patient_name"] == "Ananya Rao"


class TestAppointmentsReport:
    async def test_summary_buckets_and_breakdowns(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        response = await api.get(
            f"{REPORTS}/appointments?from=2026-10-05&to=2026-10-06", headers=await login(ADMIN)
        )

        assert response.json()["message"] == "Appointments report generated."
        report = data(response)
        assert set(report) == {
            "meta",
            "filters",
            "summary",
            "buckets",
            "by_doctor",
            "by_department",
        }
        assert report["filters"] == {
            "from": "2026-10-05",
            "to": "2026-10-06",
            "granularity": "day",
            "doctor": None,
            "department": None,
        }
        # 1 no-show of (2 completed + 1 no-show) = 33.3%.
        assert report["summary"] == {
            "total": 7,
            "booked": 1,
            "checked_in": 1,
            "in_progress": 1,
            "completed": 2,
            "cancelled": 1,
            "no_show": 1,
            "no_show_rate_percent": "33.3",
        }
        assert report["buckets"] == [
            {"bucket_start": "2026-10-05", "bucket_end": "2026-10-05", "partial": False,
             "total": 4, "booked": 0, "checked_in": 0, "in_progress": 0, "completed": 2,
             "cancelled": 1, "no_show": 1},
            {"bucket_start": "2026-10-06", "bucket_end": "2026-10-06", "partial": False,
             "total": 3, "booked": 1, "checked_in": 1, "in_progress": 1, "completed": 0,
             "cancelled": 0, "no_show": 0},
        ]  # fmt: skip
        assert [
            (r["doctor_name"], r["department_name"], r["total"], r["completed"], r["cancelled"],
             r["no_show"])
            for r in report["by_doctor"]
        ] == [
            ("Priya Sharma", "Cardiology", 3, 2, 1, 0),
            ("Arjun Nair", "Orthopaedics", 2, 0, 0, 1),
            ("Old Doc", "Cardiology", 1, 0, 0, 0),
            ("Vikram Desai", None, 1, 0, 0, 0),
        ]  # fmt: skip
        assert set(report["by_doctor"][0]) == {
            "doctor_id",
            "doctor_name",
            "department_id",
            "department_name",
            "total",
            "completed",
            "cancelled",
            "no_show",
        }
        assert report["by_doctor"][0]["doctor_id"] == str(world.priya)
        assert report["by_doctor"][3]["department_id"] is None
        assert report["by_department"] == [
            {"department_id": str(world.cardiology), "department_name": "Cardiology",
             "total": 4, "completed": 2, "cancelled": 1, "no_show": 0},
            {"department_id": str(world.orthopaedics), "department_name": "Orthopaedics",
             "total": 2, "completed": 0, "cancelled": 0, "no_show": 1},
            {"department_id": None, "department_name": None,
             "total": 1, "completed": 0, "cancelled": 0, "no_show": 0},
        ]  # fmt: skip

    async def test_doctor_and_department_filters_are_echoed_and_applied(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        headers = await login(ADMIN)
        period = "from=2026-10-05&to=2026-10-06"

        doctor = data(
            await api.get(
                f"{REPORTS}/appointments?{period}&doctor_id={world.priya}", headers=headers
            )
        )
        department = data(
            await api.get(
                f"{REPORTS}/appointments?{period}&department_id={world.cardiology}",
                headers=headers,
            )
        )
        outside = data(
            await api.get(
                f"{REPORTS}/appointments?{period}&doctor_id={world.arjun}"
                f"&department_id={world.cardiology}",
                headers=headers,
            )
        )
        retired = data(
            await api.get(
                f"{REPORTS}/appointments?{period}&doctor_id={world.retired}", headers=headers
            )
        )

        assert doctor["filters"]["doctor"] == {"id": str(world.priya), "name": "Priya Sharma"}
        assert doctor["filters"]["department"] is None
        assert (doctor["summary"]["total"], doctor["summary"]["no_show_rate_percent"]) == (3, "0.0")
        assert department["filters"]["department"] == {
            "id": str(world.cardiology),
            "name": "Cardiology",
        }
        assert department["summary"]["total"] == 4
        assert outside["summary"]["total"] == 0
        assert outside["summary"]["no_show_rate_percent"] is None
        assert (outside["by_doctor"], outside["by_department"]) == ([], [])
        assert [bucket["total"] for bucket in outside["buckets"]] == [0, 0]
        assert retired["filters"]["doctor"]["name"] == "Old Doc"
        assert retired["summary"]["total"] == 1

    async def test_week_buckets_split_sunday_from_monday(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        report = data(
            await api.get(
                f"{REPORTS}/appointments?from=2026-10-04&to=2026-10-05&granularity=week",
                headers=await login(ADMIN),
            )
        )

        assert [(b["bucket_start"], b["partial"], b["total"]) for b in report["buckets"]] == [
            ("2026-09-28", True, 1),
            ("2026-10-05", True, 4),
        ]


class TestPatientsReport:
    async def test_registrations_by_local_day_and_gender(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        response = await api.get(
            f"{REPORTS}/patients?from=2026-10-04&to=2026-10-06", headers=await login(ADMIN)
        )

        assert response.json()["message"] == "Patients report generated."
        report = data(response)
        assert set(report) == {"meta", "filters", "summary", "buckets"}
        # Active patients: 3 registered here, 1 behind the invoices, 2 in the clinic.
        assert report["summary"] == {
            "registered": 2,
            "active_total": 6,
            "by_gender": {"male": 1, "female": 1, "other": 0, "unspecified": 0},
        }
        assert report["buckets"] == [
            {"bucket_start": "2026-10-04", "bucket_end": "2026-10-04", "partial": False,
             "registered": 0},
            {"bucket_start": "2026-10-05", "bucket_end": "2026-10-05", "partial": False,
             "registered": 1},
            {"bucket_start": "2026-10-06", "bucket_end": "2026-10-06", "partial": False,
             "registered": 1},
        ]  # fmt: skip


class TestAdminDashboard:
    async def test_tiles(self, api: AsyncClient, login: Any, world: Clinic) -> None:
        response = await api.get(f"{DASH}/admin", headers=await login(ADMIN))

        assert response.json()["message"] == "Admin dashboard loaded."
        dashboard = data(response)
        assert set(dashboard) == {
            "meta",
            "appointments_today",
            "revenue_this_week",
            "patient_registrations_this_month",
        }
        assert dashboard["appointments_today"] == {
            "date": "2026-10-06",
            "total": 3,
            "booked": 1,
            "checked_in": 1,
            "in_progress": 1,
            "completed": 0,
            "cancelled": 0,
            "no_show": 0,
            "in_clinic": 2,
        }
        assert dashboard["revenue_this_week"] == {
            "from": "2026-10-05",
            "to": "2026-10-06",
            **THIS_WEEK,
        }
        assert dashboard["patient_registrations_this_month"] == {
            "from": "2026-10-01",
            "to": "2026-10-06",
            "registered": 2,
            "active_total": 6,
        }


class TestBillingDashboard:
    async def test_tiles(self, api: AsyncClient, login: Any, world: Clinic) -> None:
        response = await api.get(f"{DASH}/billing", headers=await login(BILLING))

        assert response.json()["message"] == "Billing dashboard loaded."
        dashboard = data(response)
        assert set(dashboard) == {
            "meta",
            "unpaid_invoices",
            "revenue",
            "discounts_pending_approval",
        }
        assert dashboard["unpaid_invoices"] == UNPAID
        assert dashboard["revenue"] == {
            "today": {"from": "2026-10-06", "to": "2026-10-06", **TODAY},
            "this_week": {"from": "2026-10-05", "to": "2026-10-06", **THIS_WEEK},
            "this_month": {"from": "2026-10-01", "to": "2026-10-06", **OCTOBER},
        }
        assert dashboard["discounts_pending_approval"] == {
            "invoice_count": 1,
            "discount_amount": "300.00",
        }


class TestReceptionDashboard:
    async def test_tiles(
        self, api: AsyncClient, login: Any, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor_id = await seed_reception_desk(db_session, hospital_id)

        response = await api.get(f"{DASH}/reception", headers=await login(RECEPTION))

        assert response.json()["message"] == "Reception dashboard loaded."
        dashboard = data(response)
        assert set(dashboard) == {"meta", "schedule_today", "walk_in_queue", "no_show_alerts"}
        assert dashboard["schedule_today"] == {
            "date": "2026-10-06",
            "total": 7,
            "booked": 1,
            "checked_in": 4,
            "in_progress": 1,
            "completed": 1,
            "cancelled": 0,
            "no_show": 0,
            "in_clinic": 5,
        }
        # Earliest waiting check-in 03:05:00 UTC; now 08:32:11 → 327 whole minutes.
        assert dashboard["walk_in_queue"] == {
            "waiting": 3,
            "not_arrived": 1,
            "in_consultation": 1,
            "longest_wait_minutes": 327,
        }
        alerts = dashboard["no_show_alerts"]
        assert (alerts["marked_today"], alerts["at_risk"]) == (0, 1)
        (late,) = alerts["at_risk_appointments"]
        assert set(late) == {
            "appointment_id",
            "scheduled_start",
            "minutes_late",
            "patient_id",
            "patient_name",
            "patient_mrn",
            "doctor_id",
            "doctor_name",
        }
        # Booked for 03:30:00 UTC; now 08:32:11 → 302 whole minutes late.
        assert (late["scheduled_start"], late["minutes_late"]) == ("2026-10-06T03:30:00Z", 302)
        assert (late["patient_name"], late["doctor_name"], late["doctor_id"]) == (
            "Sunita Rao",
            "Arjun Nair",
            str(doctor_id),
        )

    async def test_it_holds_no_money(
        self,
        api: AsyncClient,
        login: Any,
        world: Clinic,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        await seed_reception_desk(db_session, hospital_id)

        dashboard = data(await api.get(f"{DASH}/reception", headers=await login(RECEPTION)))

        paths = [path for path, _ in walk(dashboard)]
        assert paths
        assert not [path for path in paths if re.search(r"amount|invoice|revenue", path)]


class TestDoctorDashboard:
    @staticmethod
    async def _doctors(
        session: AsyncSession, hospital_id: uuid.UUID
    ) -> tuple[dict[str, str], uuid.UUID, uuid.UUID]:
        """Two doctors with appointments today; returns the first one's login."""
        user = await insert_user_with_permissions(session, hospital_id, [DOCTOR])
        me = await insert_named_doctor(
            session, hospital_id, first_name="-", last_name="-", user_id=user.id
        )
        colleague = await insert_named_doctor(
            session, hospital_id, first_name="Other", last_name="Colleague"
        )
        mine = await insert_report_patient(
            session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Mine"
        )
        theirs = await insert_report_patient(
            session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="Theirs"
        )
        status = AppointmentStatus
        plan = [
            (me.id, mine.id, "2026-10-06T08:00", status.IN_PROGRESS, "2026-10-06T07:50"),
            (me.id, mine.id, "2026-10-06T09:00", status.BOOKED, None),
            # 00:15 local today — 5 Oct in UTC.
            (me.id, mine.id, "2026-10-05T18:45", status.COMPLETED, None),
            # Sunday of this week, still to come; and last Sunday, the week before.
            (me.id, mine.id, "2026-10-11T05:00", status.BOOKED, None),
            (me.id, mine.id, "2026-10-04T05:00", status.COMPLETED, None),
            (colleague.id, theirs.id, "2026-10-06T08:00", status.CHECKED_IN, "2026-10-06T07:55"),
            (colleague.id, theirs.id, "2026-10-06T10:00", status.BOOKED, None),
        ]
        for doctor_id, patient_id, start, state, arrived in plan:
            await insert_report_appointment(
                session,
                hospital_id,
                patient_id=patient_id,
                doctor_id=doctor_id,
                start=utc(start),
                status=state,
                checked_in_at=utc(arrived) if arrived else None,
            )
        return auth_headers(user.id, hospital_id), me.id, theirs.id

    async def test_tiles_show_the_callers_own_day_week_and_patients(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        headers, doctor_id, _ = await self._doctors(db_session, hospital_id)

        response = await api.get(f"{DASH}/doctor", headers=headers)

        assert response.json()["message"] == "Doctor dashboard loaded."
        dashboard = data(response)
        assert set(dashboard) == {"meta", "doctor", "schedule_today", "my_patients", "this_week"}
        assert dashboard["doctor"] == {"id": str(doctor_id), "name": "Asha Menon"}
        today = dashboard["schedule_today"]
        assert {key: today[key] for key in today if key != "appointments"} == {
            "date": "2026-10-06",
            "total": 3,
            "booked": 1,
            "checked_in": 0,
            "in_progress": 1,
            "completed": 1,
            "cancelled": 0,
            "no_show": 0,
            "to_see": 1,
        }
        assert [(a["scheduled_start"], a["status"]) for a in today["appointments"]] == [
            ("2026-10-05T18:45:00Z", "completed"),
            ("2026-10-06T08:00:00Z", "in_progress"),
            ("2026-10-06T09:00:00Z", "booked"),
        ]
        assert set(today["appointments"][0]) == {
            "appointment_id",
            "scheduled_start",
            "scheduled_end",
            "status",
            "type",
            "patient_id",
            "patient_name",
            "patient_mrn",
            "checked_in_at",
        }
        assert today["appointments"][1]["checked_in_at"] == "2026-10-06T07:50:00Z"
        assert today["appointments"][2]["checked_in_at"] is None
        assert dashboard["my_patients"] == {"count": 1}
        # Monday 5 Oct to Sunday 11 Oct: today's three and Sunday's one.
        assert dashboard["this_week"] == {
            "from": "2026-10-05",
            "to": "2026-10-11",
            "total": 4,
            "booked": 2,
            "checked_in": 0,
            "in_progress": 1,
            "completed": 1,
            "cancelled": 0,
            "no_show": 0,
        }

    async def test_another_doctors_appointments_never_appear(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        headers, _, their_patient = await self._doctors(db_session, hospital_id)

        dashboard = data(await api.get(f"{DASH}/doctor", headers=headers))

        text = str(dashboard)
        assert str(their_patient) not in text
        assert "Theirs" not in text
        assert {a["patient_name"] for a in dashboard["schedule_today"]["appointments"]} == {
            "Mine Rao"
        }

    async def test_it_holds_no_money(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        await seed_billing(db_session, hospital_id)
        headers, _, _ = await self._doctors(db_session, hospital_id)

        dashboard = data(await api.get(f"{DASH}/doctor", headers=headers))

        assert not [
            path for path, _ in walk(dashboard) if re.search(r"amount|invoice|revenue", path)
        ]

    async def test_a_user_who_is_not_a_doctor_gets_404(self, api: AsyncClient, login: Any) -> None:
        response = await api.get(f"{DASH}/doctor", headers=await login(DOCTOR))

        body = response.json()
        assert response.status_code == 404
        assert body["error_code"] == "RESOURCE_NOT_FOUND"
        assert body["message"] == "No active doctor profile is linked to this account."

    async def test_a_deactivated_doctor_profile_gets_404(
        self, api: AsyncClient, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        user = await insert_user_with_permissions(db_session, hospital_id, [DOCTOR])
        await insert_named_doctor(
            db_session, hospital_id, first_name="-", last_name="-", user_id=user.id, deleted=True
        )

        response = await api.get(f"{DASH}/doctor", headers=auth_headers(user.id, hospital_id))

        assert response.status_code == 404


# ── Shapes ───────────────────────────────────────────────────────────────────


class TestShapes:
    async def test_every_response_is_enveloped_typed_and_stamped(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        headers = await login(*ALL_REPORT_CODES, doctor=True)

        for path in PATHS[:8]:
            response = await api.get(path, headers=headers)
            body = response.json()
            assert set(body) >= {"success", "message", "data"}, path
            payload = data(response)
            assert set(payload["meta"]) == META_KEYS, path
            assert GENERATED_AT.match(payload["meta"]["generated_at"]), path
            for leaf_path, value in walk(payload):
                name = leaf_path.rsplit(".", 1)[-1]
                if name.endswith("_amount") or name in {"total", "amount_paid", "balance_due"}:
                    if name == "total" and isinstance(value, int):
                        continue  # an appointment count, not an invoice total
                    assert_money(value)
                if name.endswith("_count") or name in COUNT_KEYS - {"total"}:
                    assert type(value) is int, f"{path} {leaf_path}={value!r}"
                assert not isinstance(value, float), f"{path} {leaf_path} is a float"

    async def test_bucket_and_summary_key_sets(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        headers = await login(ADMIN)

        revenue = data(await api.get(f"{REPORTS}/revenue", headers=headers))
        appointments = data(await api.get(f"{REPORTS}/appointments", headers=headers))
        patients = data(await api.get(f"{REPORTS}/patients", headers=headers))
        outstanding = data(await api.get(f"{REPORTS}/outstanding", headers=headers))
        admin = data(await api.get(f"{DASH}/admin", headers=headers))

        assert set(revenue["summary"]) == REVENUE_KEYS
        assert set(revenue["buckets"][0]) == BUCKET_KEYS | REVENUE_KEYS
        assert set(revenue["by_method"][0]) == {
            "method",
            "payment_count",
            "collected_amount",
            "refund_count",
            "refunded_amount",
            "net_collected_amount",
        }
        assert set(appointments["summary"]) == COUNT_KEYS | {"no_show_rate_percent"}
        assert set(appointments["buckets"][0]) == BUCKET_KEYS | COUNT_KEYS
        assert set(patients["summary"]) == {"registered", "active_total", "by_gender"}
        assert set(patients["buckets"][0]) == BUCKET_KEYS | {"registered"}
        assert set(outstanding["summary"]) == OUTSTANDING_KEYS
        assert set(outstanding["ageing"][0]) == {
            "bucket",
            "min_days",
            "max_days",
            "invoice_count",
            "outstanding_amount",
        }
        assert set(admin["appointments_today"]) == COUNT_KEYS | {"date", "in_clinic"}
        assert set(admin["revenue_this_week"]) == REVENUE_KEYS | {"from", "to"}
        for removed in ("by_type", "subtotal_amount", "discount_amount", "tax_amount"):
            assert removed not in appointments["summary"]
            assert removed not in revenue["summary"]

    async def test_filters_echo_the_defaults(self, api: AsyncClient, login: Any) -> None:
        headers = await login(ADMIN)

        for report in ("patients", "appointments", "revenue"):
            filters = data(await api.get(f"{REPORTS}/{report}", headers=headers))["filters"]
            assert (filters["from"], filters["to"], filters["granularity"]) == (
                "2026-09-07",
                "2026-10-06",
                "day",
            )


# ── Empty hospital and tenant isolation ──────────────────────────────────────


def assert_empty(path: str, payload: dict[str, Any]) -> None:
    """Assert a response reports nothing: zero counts, zero money, empty lists."""
    for leaf_path, value in walk(payload):
        if leaf_path.startswith((".meta", ".filters", ".doctor")):
            continue
        name = leaf_path.rsplit(".", 1)[-1]
        if name in {"date", "from", "to", "bucket_start", "bucket_end", "partial", "bucket", "method",
                    "min_days", "max_days"}:  # fmt: skip
            continue
        if name in {"no_show_rate_percent", "longest_wait_minutes"}:
            assert value is None, f"{path} {leaf_path}={value!r}"
        elif name == "invoices_truncated":
            assert value is False
        elif isinstance(value, str):
            assert value == "0.00", f"{path} {leaf_path}={value!r}"
        else:
            assert value == 0, f"{path} {leaf_path}={value!r}"


class TestEmptyHospital:
    async def test_every_route_answers_200_with_zeros(self, api: AsyncClient, login: Any) -> None:
        headers = await login(*ALL_REPORT_CODES, doctor=True)

        for path in PATHS[:8]:
            payload = data(await api.get(path, headers=headers))
            if path == f"{REPORTS}/patients":
                # The caller's own account is not a patient; nobody is registered.
                assert payload["summary"]["active_total"] == 0
            assert_empty(path, payload)

        revenue = data(await api.get(f"{REPORTS}/revenue", headers=headers))
        outstanding = data(await api.get(f"{REPORTS}/outstanding", headers=headers))
        appointments = data(await api.get(f"{REPORTS}/appointments", headers=headers))
        reception = data(await api.get(f"{DASH}/reception", headers=headers))
        doctor = data(await api.get(f"{DASH}/doctor", headers=headers))
        assert len(revenue["buckets"]) == 30
        assert len(revenue["by_method"]) == 5
        assert len(outstanding["ageing"]) == 4
        assert outstanding["invoices"] == []
        assert (appointments["by_doctor"], appointments["by_department"]) == ([], [])
        assert reception["no_show_alerts"]["at_risk_appointments"] == []
        assert doctor["schedule_today"]["appointments"] == []

    @pytest.mark.parametrize("report", ["patients", "appointments", "revenue", "outstanding"])
    async def test_an_empty_export_is_headers_and_a_zero_total(
        self, api: AsyncClient, login: Any, report: str
    ) -> None:
        rows = csv_rows(
            await api.get(f"{REPORTS}/{report}/export", headers=await login(ADMIN, EXPORT))
        )

        assert rows[0][0] == "Hospital"
        assert rows[-1][0] == "Total"
        assert set(rows[-1][1:]) <= {"", "0", "0.00"}


class TestTenantIsolation:
    async def test_a_user_of_another_hospital_sees_none_of_this_hospitals_data(
        self,
        api: AsyncClient,
        login: Any,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await seed_billing(db_session, hospital_id)
        await seed_clinic(db_session, hospital_id)
        await seed_registrations(db_session, hospital_id)
        await seed_reception_desk(db_session, hospital_id)
        outsider = await login(*ALL_REPORT_CODES, doctor=True, hospital=other_hospital_id)
        insider = await login(*ALL_REPORT_CODES, doctor=True)

        for path in PATHS[:8]:
            assert_empty(path, data(await api.get(path, headers=outsider)))
        for report in ("patients", "appointments", "revenue", "outstanding"):
            rows = csv_rows(await api.get(f"{REPORTS}/{report}/export", headers=outsider))
            assert set(rows[-1][1:]) <= {"", "0", "0.00"}, report
            assert "Ananya" not in str(rows)

        # The same requests by a user of the hospital do return the data.
        assert (
            data(await api.get(f"{REPORTS}/revenue", headers=insider))["summary"]["invoice_count"]
            == 6
        )
        assert data(await api.get(f"{REPORTS}/outstanding", headers=insider))["invoices"]

    async def test_each_hospital_gets_its_own_figures_when_both_hold_data(
        self,
        api: AsyncClient,
        login: Any,
        world: Clinic,
        other_hospital_id: uuid.UUID,
    ) -> None:
        ours = await login(ADMIN)
        theirs = await login(ADMIN, hospital=other_hospital_id)
        query = "from=2026-10-01&to=2026-10-06"

        our_revenue = data(await api.get(f"{REPORTS}/revenue?{query}", headers=ours))
        their_revenue = data(await api.get(f"{REPORTS}/revenue?{query}", headers=theirs))
        their_outstanding = data(await api.get(f"{REPORTS}/outstanding", headers=theirs))
        their_appointments = data(await api.get(f"{REPORTS}/appointments?{query}", headers=theirs))

        assert our_revenue["summary"] == OCTOBER
        assert their_revenue["summary"] == {
            "invoice_count": 2,
            "invoiced_amount": "88.88",
            "payment_count": 1,
            "collected_amount": "11.11",
            "refund_count": 1,
            "refunded_amount": "5.55",
            "net_collected_amount": "5.56",
        }
        assert our_revenue["meta"]["hospital_name"] != their_revenue["meta"]["hospital_name"]
        assert their_outstanding["summary"]["outstanding_amount"] == "77.77"
        assert len(their_outstanding["invoices"]) == 1
        # 2 no-shows of (1 completed + 2 no-shows) = 66.7%.
        assert their_appointments["summary"]["no_show_rate_percent"] == "66.7"
        assert [row["doctor_name"] for row in their_appointments["by_doctor"]] == ["Other Doctor"]

    async def test_a_filter_naming_another_hospitals_doctor_leaks_nothing(
        self,
        api: AsyncClient,
        login: Any,
        world: Clinic,
        other_hospital_id: uuid.UUID,
    ) -> None:
        theirs = await login(ADMIN, hospital=other_hospital_id)

        response = await api.get(f"{REPORTS}/appointments?doctor_id={world.priya}", headers=theirs)

        assert rule_error(response) == (422, "Doctor not found.", "doctor_id")
        assert "Priya" not in response.text


# ── Export ───────────────────────────────────────────────────────────────────


class TestExport:
    async def test_a_revenue_export_is_a_csv_download_matching_the_report(
        self, api: AsyncClient, login: Any, world: Clinic, audit: RecordingAuditSink
    ) -> None:
        headers = await login(BILLING, EXPORT)
        query = "from=2026-10-01&to=2026-10-06"

        response = await api.get(f"{REPORTS}/revenue/export?{query}", headers=headers)
        summary = data(await api.get(f"{REPORTS}/revenue?{query}", headers=headers))["summary"]

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/csv; charset=utf-8"
        assert response.headers["content-disposition"] == (
            'attachment; filename="revenue-report-2026-10-01_to_2026-10-06-20261006-083211.csv"'
        )
        assert b"\r\n" in response.content
        rows = csv_rows(response)
        hospital_name = rows[0][1]
        assert rows[:9] == [
            ["Hospital", hospital_name],
            ["Report", "Revenue"],
            ["Period", "2026-10-01 to 2026-10-06"],
            ["Granularity", "day"],
            ["Timezone", "Asia/Kolkata"],
            ["Currency", "INR"],
            ["Generated at", "2026-10-06T14:02:11+05:30"],
            [],
            [
                "bucket_start",
                "bucket_end",
                "invoice_count",
                "invoiced_amount",
                "payment_count",
                "collected_amount",
                "refund_count",
                "refunded_amount",
                "net_collected_amount",
            ],
        ]
        assert hospital_name.startswith("Test Hospital")
        assert len(rows) == 9 + 6 + 1
        assert rows[13] == ["2026-10-05", "2026-10-05", "3", "1850.10", "4", "300.10", "0", "0.00",
                            "300.10"]  # fmt: skip
        assert rows[-1] == ["Total", "", "6", "4350.10", "7", "2300.10", "2", "950.00", "1350.10"]
        assert rows[-1][2:] == [str(summary[column]) for column in rows[8][2:]]

        assert audit.actions() == ["report.exported"]
        event = audit.last()
        assert event.target_type == "report"
        assert event.target_id is None
        assert event.actor_id is not None
        assert event.context == {
            "report_id": "revenue",
            "format": "csv",
            "from": "2026-10-01",
            "to": "2026-10-06",
            "granularity": "day",
            "row_count": 6,
        }

    @pytest.mark.parametrize(
        ("report", "query", "pattern"),
        [
            ("patients", "", r"patients-report-2026-09-07_to_2026-10-06-20261006-083211\.csv"),
            (
                "appointments",
                "granularity=week",
                r"appointments-report-2026-09-07_to_2026-10-06-20261006-083211\.csv",
            ),
            (
                "revenue",
                "format=csv",
                r"revenue-report-2026-09-07_to_2026-10-06-20261006-083211\.csv",
            ),
            (
                "revenue",
                "format=CSV",
                r"revenue-report-2026-09-07_to_2026-10-06-20261006-083211\.csv",
            ),
            ("outstanding", "", r"outstanding-report-as-of-2026-10-06-20261006-083211\.csv"),
        ],
    )
    async def test_each_report_exports_with_its_filename_and_totals(
        self,
        api: AsyncClient,
        login: Any,
        world: Clinic,
        audit: RecordingAuditSink,
        report: str,
        query: str,
        pattern: str,
    ) -> None:
        headers = await login(ADMIN, EXPORT)
        report_query = "&".join(part for part in query.split("&") if not part.startswith("format"))

        response = await api.get(f"{REPORTS}/{report}/export?{query}", headers=headers)
        payload = data(await api.get(f"{REPORTS}/{report}?{report_query}", headers=headers))

        assert response.headers["content-type"].startswith("text/csv")
        assert re.fullmatch(
            f'attachment; filename="{pattern}"', response.headers["content-disposition"]
        )
        rows = csv_rows(response)
        blank = rows.index([])
        header, total = rows[blank + 1], rows[-1]
        assert total[0] == "Total"
        summary = payload["summary"]
        if report == "outstanding":
            assert total[-1] == summary["outstanding_amount"] == "1550.00"
            assert len(rows) - blank - 3 == len(payload["invoices"]) == 2
            assert ["Rows", "2"] in rows
            assert ["As of", "2026-10-06"] in rows
        else:
            assert total[2:] == [str(summary[column]) for column in header[2:]]
            assert len(rows) - blank - 3 == len(payload["buckets"])
        assert audit.actions() == ["report.exported"]
        assert audit.last().context["report_id"] == report

    async def test_an_appointments_export_names_its_filters(
        self, api: AsyncClient, login: Any, world: Clinic, audit: RecordingAuditSink
    ) -> None:
        response = await api.get(
            f"{REPORTS}/appointments/export?from=2026-10-05&to=2026-10-06"
            f"&doctor_id={world.priya}&department_id={world.cardiology}",
            headers=await login(ADMIN, EXPORT),
        )

        rows = csv_rows(response)
        assert ["Doctor", "Priya Sharma"] in rows
        assert ["Department", "Cardiology"] in rows
        assert rows[-1] == ["Total", "", "3", "0", "0", "0", "2", "1", "0"]
        assert audit.last().context == {
            "report_id": "appointments",
            "format": "csv",
            "doctor_id": str(world.priya),
            "department_id": str(world.cardiology),
            "from": "2026-10-05",
            "to": "2026-10-06",
            "granularity": "day",
            "row_count": 2,
        }

    async def test_an_outstanding_export_lists_the_unpaid_invoices(
        self, api: AsyncClient, login: Any, world: Clinic
    ) -> None:
        rows = csv_rows(
            await api.get(f"{REPORTS}/outstanding/export", headers=await login(BILLING, EXPORT))
        )

        blank = rows.index([])
        assert rows[blank + 1] == [
            "invoice_number",
            "issued_date",
            "age_days",
            "patient_mrn",
            "patient_name",
            "status",
            "total",
            "amount_paid",
            "balance_due",
        ]
        first, second = rows[blank + 2 : -1]
        assert first[1:3] == ["2026-10-05", "1"]
        assert first[4:] == ["Ananya Rao", "partially_paid", "750.00", "300.00", "450.00"]
        assert second[5:] == ["issued", "1100.00", "0.00", "1100.00"]
        assert rows[-1] == ["Total", "", "", "", "", "", "", "", "1550.00"]

    async def test_the_audit_entry_is_really_written_to_the_trail(
        self,
        application: FastAPI,
        api: AsyncClient,
        login: Any,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """With the real audit service, not the recording sink."""
        from sqlalchemy import select

        from app.models.audit_log import AuditLog

        del application.dependency_overrides[get_audit_sink]

        response = await api.get(
            f"{REPORTS}/outstanding/export", headers=await login(BILLING, EXPORT)
        )

        assert response.status_code == 200
        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.hospital_id == hospital_id, AuditLog.action == "report.exported"
            )
        )
        (entry,) = result.unique().scalars().all()
        assert entry.target_type == "report"
        assert entry.context == {"report_id": "outstanding", "format": "csv", "row_count": 0}

    @pytest.mark.parametrize(
        ("requested", "message"),
        [
            ("pdf", "PDF export is not available yet. Use format=csv."),
            ("PDF", "PDF export is not available yet. Use format=csv."),
            ("xlsx", "Unsupported export format `xlsx`. Use format=csv."),
            ("json", "Unsupported export format `json`. Use format=csv."),
        ],
    )
    async def test_any_other_format_is_refused(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink, requested: str, message: str
    ) -> None:
        response = await api.get(
            f"{REPORTS}/revenue/export?format={requested}", headers=await login(BILLING, EXPORT)
        )

        assert rule_error(response) == (422, message, "format")
        assert audit.events == []

    async def test_export_needs_both_the_export_code_and_the_reports_read_code(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink
    ) -> None:
        export_only = await api.get(f"{REPORTS}/revenue/export", headers=await login(EXPORT))
        read_only = await api.get(f"{REPORTS}/revenue/export", headers=await login(BILLING, ADMIN))
        wrong_read = await api.get(
            f"{REPORTS}/patients/export", headers=await login(EXPORT, BILLING)
        )

        assert export_only.status_code == 403
        assert export_only.json()["message"] == (
            "Permission denied. Required one of: report.admin.read, report.billing.read."
        )
        assert export_only.json()["error_code"] == "PERMISSION_DENIED"
        assert read_only.status_code == 403
        assert read_only.json()["message"] == "Permission denied. Required: report.export."
        assert wrong_read.status_code == 403
        assert wrong_read.json()["message"] == "Permission denied. Required: report.admin.read."
        assert audit.events == []

    async def test_a_failed_export_records_nothing(
        self, api: AsyncClient, login: Any, audit: RecordingAuditSink
    ) -> None:
        headers = await login(ADMIN, EXPORT)

        for path in (
            f"{REPORTS}/inventory/export",
            f"{REPORTS}/revenue/export?granularity=hour",
            f"{REPORTS}/revenue/export?from=2026-10-06&to=2026-10-05",
            f"{REPORTS}/appointments/export?doctor_id={uuid.uuid4()}",
            f"{REPORTS}/outstanding/export?from=2026-10-01",
            f"{REPORTS}/revenue/export?from=garbage",
        ):
            response = await api.get(path, headers=headers)
            assert response.status_code in (404, 422), path

        assert audit.events == []

    async def test_a_name_that_is_a_formula_is_neutralised_in_the_file(
        self, api: AsyncClient, login: Any, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        from app.models.billing import InvoiceStatus
        from app.tests.report_helpers import insert_report_invoice

        patient = await insert_report_patient(
            db_session, hospital_id, created_at=REGISTERED_LONG_AGO, first_name="=SUM(A1)"
        )
        await insert_report_invoice(
            db_session,
            hospital_id,
            patient_id=patient.id,
            status=InvoiceStatus.ISSUED,
            total="10.00",
            issued_at=utc("2026-10-05T05:00"),
        )

        rows = csv_rows(
            await api.get(f"{REPORTS}/outstanding/export", headers=await login(BILLING, EXPORT))
        )

        assert rows[rows.index([]) + 2][4] == "'=SUM(A1) Rao"
