"""Reports and Dashboards API routes.

Implements ``docs/modules/10-reports-dashboard.md`` §9. Routes parse input,
delegate to :class:`~app.services.report_service.ReportService`, and wrap the
result in the standard envelope. No aggregation, no arithmetic, no database
access.

Two routers live here: ``/dashboards`` (one per role) and ``/reports`` (four
reports and their export).

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Order of checks.** The permission dependency answers first (401, 403). A
malformed date or UUID is then a 422 from FastAPI. Only then does the route
body run: tenant (400), on export the report id (404) and the report's own
read permission (403), then unknown query parameters (422). The service
validates everything else.

**Unknown query parameters are refused,** so a mistyped filter is a 422 rather
than a report silently computed without it.

**Not here.** AI summaries, PDF export (``format=pdf`` is a 422) and scheduled
delivery are not built.
"""

from __future__ import annotations

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import Response

from app.api.dependencies.auth import (
    require_any_permission,
    require_permission,
    user_has_permission,
)
from app.api.dependencies.services import get_report_service
from app.core.exceptions import BusinessRuleError, NotFoundError, PermissionDeniedError
from app.models.user import User
from app.schemas.common import SuccessResponse
from app.schemas.report import (
    AdminDashboard,
    AppointmentsReport,
    BillingDashboard,
    DoctorDashboard,
    OutstandingReport,
    PatientsReport,
    ReceptionDashboard,
    RevenueReport,
)
from app.services.report_periods import field_error
from app.services.report_service import ReportService

dashboard_router = APIRouter(prefix="/dashboards", tags=["Dashboards"])
router = APIRouter(prefix="/reports", tags=["Reports"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    400: {"description": "The account is not scoped to a hospital."},
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "A query parameter is unknown or failed validation."},
}

_ADMIN = "report.admin.read"
_DOCTOR = "report.doctor.read"
_RECEPTION = "report.reception.read"
_BILLING = "report.billing.read"
_EXPORT = "report.export"

#: The permission codes that open each report; holding any one is enough.
#: The export route requires ``report.export`` **and** one of these.
REPORT_READ_CODES: dict[str, tuple[str, ...]] = {
    "patients": (_ADMIN,),
    "appointments": (_ADMIN,),
    "revenue": (_ADMIN, _BILLING),
    "outstanding": (_ADMIN, _BILLING),
}

_PERIOD_PARAMS = frozenset({"from", "to", "granularity"})

#: The query parameters each report accepts. Anything else is a 422.
REPORT_PARAMS: dict[str, frozenset[str]] = {
    "patients": _PERIOD_PARAMS,
    "appointments": _PERIOD_PARAMS | {"doctor_id", "department_id"},
    "revenue": _PERIOD_PARAMS,
    "outstanding": frozenset(),
}

_NO_PARAMS: frozenset[str] = frozenset()

_FROM = "First date of the period (YYYY-MM-DD, hospital-local). Default: 29 days before `to`."
_TO = "Last date of the period (YYYY-MM-DD, hospital-local). Default: today."
_GRANULARITY = "Bucket size: `day` (default), `week` (from Monday) or `month`."


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so reports cannot be read."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


def _reject_unknown_params(request: Request, allowed: frozenset[str]) -> None:
    """Refuse a request carrying a query parameter the route does not define.

    A parameter given twice is refused as well: the framework would silently
    keep the last value, and a report must not guess which one was meant.

    :param request: The incoming request.
    :param allowed: The query parameter names the route accepts.
    :raises ValidationError: Naming the first unknown or repeated parameter, in
        request order.
    """
    seen: set[str] = set()
    for key, _ in request.query_params.multi_items():
        if key not in allowed:
            raise field_error(key, f"Unknown query parameter: `{key}`.")
        if key in seen:
            raise field_error(key, f"Query parameter `{key}` must be given only once.")
        seen.add(key)


def _require_report_read(current_user: User, report_id: str) -> None:
    """Refuse a caller who may not read the report they are exporting.

    :param current_user: The authenticated user.
    :param report_id: A known report id.
    :raises PermissionDeniedError: If the user holds none of the report's read codes.
    """
    codes = REPORT_READ_CODES[report_id]
    if any(user_has_permission(current_user, code) for code in codes):
        return
    required = (
        f"Required: {codes[0]}." if len(codes) == 1 else f"Required one of: {', '.join(codes)}."
    )
    raise PermissionDeniedError(message=f"Permission denied. {required}")


# ── Dashboards ──────────────────────────────────────────────────────────────


@dashboard_router.get(
    "/admin",
    response_model=SuccessResponse[AdminDashboard],
    summary="Admin dashboard",
    description=(
        "Today's appointments, revenue from Monday to today, and patient "
        "registrations from the 1st of the month to today, in the hospital's timezone."
    ),
    responses={200: {"description": "Dashboard returned."}, **_COMMON_RESPONSES},
)
async def get_admin_dashboard(
    request: Request,
    current_user: User = Depends(require_permission(_ADMIN)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[AdminDashboard]:
    """Return the hospital admin's dashboard (module spec FR-1)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, _NO_PARAMS)
    dashboard = await service.admin_dashboard(hospital_id)
    return SuccessResponse[AdminDashboard](message="Admin dashboard loaded.", data=dashboard)


@dashboard_router.get(
    "/doctor",
    response_model=SuccessResponse[DoctorDashboard],
    summary="Doctor dashboard",
    description=(
        "The caller's own schedule today, their distinct patients, and their "
        "appointments this week. Scoped to the caller's active doctor profile; holds no money."
    ),
    responses={
        200: {"description": "Dashboard returned."},
        404: {"description": "No active doctor profile is linked to this account."},
        **_COMMON_RESPONSES,
    },
)
async def get_doctor_dashboard(
    request: Request,
    current_user: User = Depends(require_permission(_DOCTOR)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[DoctorDashboard]:
    """Return the calling doctor's dashboard (module spec FR-2)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, _NO_PARAMS)
    dashboard = await service.doctor_dashboard(hospital_id, current_user.id)
    return SuccessResponse[DoctorDashboard](message="Doctor dashboard loaded.", data=dashboard)


@dashboard_router.get(
    "/reception",
    response_model=SuccessResponse[ReceptionDashboard],
    summary="Reception dashboard",
    description=(
        "Today's schedule, the walk-in queue, and no-show alerts: no-shows "
        "recorded today and booked appointments already past their start. Holds no money."
    ),
    responses={200: {"description": "Dashboard returned."}, **_COMMON_RESPONSES},
)
async def get_reception_dashboard(
    request: Request,
    current_user: User = Depends(require_permission(_RECEPTION)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[ReceptionDashboard]:
    """Return the reception dashboard (module spec FR-3)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, _NO_PARAMS)
    dashboard = await service.reception_dashboard(hospital_id)
    return SuccessResponse[ReceptionDashboard](
        message="Reception dashboard loaded.", data=dashboard
    )


@dashboard_router.get(
    "/billing",
    response_model=SuccessResponse[BillingDashboard],
    summary="Billing dashboard",
    description=(
        "Unpaid invoices, revenue today, this week and this month, and "
        "discounts waiting for approval."
    ),
    responses={200: {"description": "Dashboard returned."}, **_COMMON_RESPONSES},
)
async def get_billing_dashboard(
    request: Request,
    current_user: User = Depends(require_permission(_BILLING)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[BillingDashboard]:
    """Return the billing dashboard (module spec FR-4)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, _NO_PARAMS)
    dashboard = await service.billing_dashboard(hospital_id)
    return SuccessResponse[BillingDashboard](message="Billing dashboard loaded.", data=dashboard)


# ── Reports ─────────────────────────────────────────────────────────────────
# The four literal paths are declared before `/{report_id}/export`.


@router.get(
    "/patients",
    response_model=SuccessResponse[PatientsReport],
    summary="Patients report",
    description=(
        "Active patients registered in the period, per bucket and by gender. "
        "The period is at most 12 months."
    ),
    responses={200: {"description": "Report returned."}, **_COMMON_RESPONSES},
)
async def get_patients_report(
    request: Request,
    start: date | None = Query(None, alias="from", description=_FROM),
    end: date | None = Query(None, alias="to", description=_TO),
    granularity: str | None = Query(None, description=_GRANULARITY),
    current_user: User = Depends(require_permission(_ADMIN)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[PatientsReport]:
    """Return the patients report (module spec FR-5)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, REPORT_PARAMS["patients"])
    report = await service.patients_report(
        hospital_id, from_=start, to=end, granularity=granularity
    )
    return SuccessResponse[PatientsReport](message="Patients report generated.", data=report)


@router.get(
    "/appointments",
    response_model=SuccessResponse[AppointmentsReport],
    summary="Appointments report",
    description=(
        "Appointments scheduled in the period by status, per bucket, per "
        "doctor and per department, with the no-show rate. Optionally narrowed "
        "to one doctor, one department, or both."
    ),
    responses={200: {"description": "Report returned."}, **_COMMON_RESPONSES},
)
async def get_appointments_report(
    request: Request,
    start: date | None = Query(None, alias="from", description=_FROM),
    end: date | None = Query(None, alias="to", description=_TO),
    granularity: str | None = Query(None, description=_GRANULARITY),
    doctor_id: uuid.UUID | None = Query(None, description="Only this doctor's appointments."),
    department_id: uuid.UUID | None = Query(
        None, description="Only appointments of doctors in this department."
    ),
    current_user: User = Depends(require_permission(_ADMIN)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[AppointmentsReport]:
    """Return the appointments report (module spec FR-5)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, REPORT_PARAMS["appointments"])
    report = await service.appointments_report(
        hospital_id,
        from_=start,
        to=end,
        granularity=granularity,
        doctor_id=doctor_id,
        department_id=department_id,
    )
    return SuccessResponse[AppointmentsReport](
        message="Appointments report generated.", data=report
    )


@router.get(
    "/revenue",
    response_model=SuccessResponse[RevenueReport],
    summary="Revenue report",
    description=(
        "What was billed (by issue date), collected and refunded (by the date "
        "the money moved) in the period, per bucket and per payment method."
    ),
    responses={200: {"description": "Report returned."}, **_COMMON_RESPONSES},
)
async def get_revenue_report(
    request: Request,
    start: date | None = Query(None, alias="from", description=_FROM),
    end: date | None = Query(None, alias="to", description=_TO),
    granularity: str | None = Query(None, description=_GRANULARITY),
    current_user: User = Depends(require_any_permission(_ADMIN, _BILLING)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[RevenueReport]:
    """Return the revenue report (module spec FR-5)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, REPORT_PARAMS["revenue"])
    report = await service.revenue_report(hospital_id, from_=start, to=end, granularity=granularity)
    return SuccessResponse[RevenueReport](message="Revenue report generated.", data=report)


@router.get(
    "/outstanding",
    response_model=SuccessResponse[OutstandingReport],
    summary="Outstanding invoices report",
    description=(
        "What is owed on issued and part-paid invoices as of now, by age, with "
        "the oldest 100 invoices. Takes no query parameters."
    ),
    responses={200: {"description": "Report returned."}, **_COMMON_RESPONSES},
)
async def get_outstanding_report(
    request: Request,
    current_user: User = Depends(require_any_permission(_ADMIN, _BILLING)),
    service: ReportService = Depends(get_report_service),
) -> SuccessResponse[OutstandingReport]:
    """Return the outstanding invoices report (module spec FR-5)."""
    hospital_id = _tenant_of(current_user)
    _reject_unknown_params(request, REPORT_PARAMS["outstanding"])
    report = await service.outstanding_report(hospital_id)
    return SuccessResponse[OutstandingReport](message="Outstanding report generated.", data=report)


@router.get(
    "/{report_id}/export",
    response_class=Response,
    summary="Export a report as CSV",
    description=(
        "Download a report as a CSV file (`?format=csv`, the default and the "
        "only format). Takes the same query parameters as the report. Needs "
        "`report.export` and the report's own read permission. The export is "
        "recorded in the audit trail."
    ),
    responses={
        200: {
            "description": "File download (content-disposition attached).",
            "content": {"text/csv": {}},
        },
        404: {"description": "No such report."},
        **_COMMON_RESPONSES,
    },
)
async def export_report(
    request: Request,
    report_id: str = Path(description="`patients`, `appointments`, `revenue` or `outstanding`."),
    format: str | None = Query(None, description="Export format. Only `csv` is available."),
    start: date | None = Query(None, alias="from", description=_FROM),
    end: date | None = Query(None, alias="to", description=_TO),
    granularity: str | None = Query(None, description=_GRANULARITY),
    doctor_id: uuid.UUID | None = Query(None, description="Appointments report only."),
    department_id: uuid.UUID | None = Query(None, description="Appointments report only."),
    current_user: User = Depends(require_permission(_EXPORT)),
    service: ReportService = Depends(get_report_service),
) -> Response:
    """Export one report as a CSV download (module spec FR-6)."""
    hospital_id = _tenant_of(current_user)
    if report_id not in REPORT_READ_CODES:
        raise NotFoundError(message=f"Unknown report: `{report_id}`.")
    _require_report_read(current_user, report_id)
    _reject_unknown_params(request, REPORT_PARAMS[report_id] | {"format"})

    export = await service.export_report(
        hospital_id,
        report_id,
        actor_id=current_user.id,
        format=format,
        from_=start,
        to=end,
        granularity=granularity,
        doctor_id=doctor_id,
        department_id=department_id,
    )
    return Response(
        content=export.content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{export.filename}"'},
    )
