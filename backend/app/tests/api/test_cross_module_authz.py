"""Cross-module authorization matrix (20-day plan, Days 14–17).

One test family walks every major module's read endpoint and asserts both
directions — denied without the permission, served with it — and a second
family asserts the mutating endpoints refuse a permission-less caller.

This is deliberately *not* per-module: the per-module suites prove their own
happy paths, this proves the seam between them, where a router can quietly
lose its ``require_permission`` guard during a refactor and every individual
suite still passes because they authenticate as a privileged fixture.

Modules covered: Patients, Doctors, Appointments, Departments, Users, Roles,
Hospital Settings, Audit Logs — i.e. every router mounted on the app today
(``backend/app/main.py``). Billing/Lab/Pharmacy/Inventory have no backend
router yet, so there is nothing to guard.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.api.dependencies.db import get_db_session
from app.core.security import create_access_token
from app.main import create_app
from app.models.user import User
from app.tests.conftest import grant_permissions

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database

#: (path, permission) for every guarded list endpoint in the application.
READ_ENDPOINTS: list[tuple[str, str]] = [
    ("/api/v1/patients", "patient.read"),
    ("/api/v1/doctors", "doctor.read"),
    ("/api/v1/appointments", "appointment.read"),
    ("/api/v1/departments", "department.read"),
    ("/api/v1/users", "user.read"),
    ("/api/v1/roles", "role.read"),
    ("/api/v1/hospitals/current/full", "settings.read"),
    ("/api/v1/audit-logs", "audit.read"),
    # Reports and dashboards. `/dashboards/doctor` is deliberately absent: a
    # holder of its code who is not a doctor is answered 404, so it cannot sit
    # in a table that asserts 200 for a bare user. See
    # `test_doctor_dashboard_denied_without_permission` below. The export route
    # needs two codes and is covered in `test_reports_api.py`.
    ("/api/v1/dashboards/admin", "report.admin.read"),
    ("/api/v1/dashboards/reception", "report.reception.read"),
    ("/api/v1/dashboards/billing", "report.billing.read"),
    ("/api/v1/reports/patients", "report.admin.read"),
    ("/api/v1/reports/appointments", "report.admin.read"),
    ("/api/v1/reports/revenue", "report.billing.read"),
    ("/api/v1/reports/outstanding", "report.billing.read"),
]

#: (method, path, permission, body) for guarded mutating endpoints. The body
#: is valid JSON; 403 must win over validation because the permission gate is
#: the outermost check. The method matters — routing rejects a wrong method
#: (405) before any dependency runs.
MUTATION_ENDPOINTS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("POST", "/api/v1/patients", "patient.create", {}),
    ("POST", "/api/v1/doctors", "doctor.create", {}),
    ("POST", "/api/v1/departments", "department.create", {}),
    ("POST", "/api/v1/users", "user.create", {}),
    (
        "POST",
        f"/api/v1/users/{uuid.uuid4()}/roles",
        "role.assign",
        {"role_id": str(uuid.uuid4())},
    ),
    ("PATCH", "/api/v1/hospitals/current", "settings.update", {"name": "Nope Hospital"}),
]


async def _make_user(
    session: AsyncSession, hospital_id: uuid.UUID, permissions: list[str]
) -> uuid.UUID:
    """Create an active user in ``hospital_id`` holding ``permissions``."""
    user = User(
        id=uuid.uuid4(),
        hospital_id=hospital_id,
        email=f"matrix-{uuid.uuid4().hex[:12]}@hospital.test",
        password_hash="test-placeholder-not-a-hash",
        first_name="Matrix",
        last_name="Tester",
    )
    session.add(user)
    await session.flush()
    if permissions:
        await grant_permissions(
            session, hospital_id=hospital_id, user_id=user.id, codes=permissions
        )
    return user.id


def _headers(user_id: uuid.UUID, hospital_id: uuid.UUID) -> dict[str, str]:
    """Mint a Bearer header for a user."""
    token = create_access_token(user_id=user_id, hospital_id=hospital_id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def api(db_session: AsyncSession) -> AsyncGenerator[AsyncClient]:
    """An HTTP client whose requests share the test's rolled-back session."""
    application: FastAPI = create_app()

    async def _session_override() -> AsyncGenerator[AsyncSession]:
        yield db_session

    application.dependency_overrides[get_db_session] = _session_override
    transport = ASGITransport(app=application)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    application.dependency_overrides.clear()


@pytest_asyncio.fixture
async def no_permissions(db_session: AsyncSession, hospital_id: uuid.UUID) -> dict[str, str]:
    """Auth header for an active user who holds no permissions at all."""
    user_id = await _make_user(db_session, hospital_id, [])
    return _headers(user_id, hospital_id)


@pytest.mark.parametrize(("path", "permission"), READ_ENDPOINTS)
async def test_read_denied_without_permission(
    api: AsyncClient,
    no_permissions: dict[str, str],
    path: str,
    permission: str,
) -> None:
    """Every guarded list endpoint refuses a caller without its permission."""
    response = await api.get(path, headers=no_permissions)
    assert response.status_code == 403, f"GET {path} let a caller without {permission} in"
    assert response.json()["error_code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(("path", "permission"), READ_ENDPOINTS)
async def test_read_served_with_permission(
    api: AsyncClient,
    db_session: AsyncSession,
    hospital_id: uuid.UUID,
    path: str,
    permission: str,
) -> None:
    """The same endpoints serve a caller who holds exactly that permission."""
    headers = _headers(await _make_user(db_session, hospital_id, [permission]), hospital_id)
    response = await api.get(path, headers=headers)
    assert response.status_code == 200, f"{path} denied a legitimate holder of {permission}"


async def test_doctor_dashboard_denied_without_permission(
    api: AsyncClient,
    no_permissions: dict[str, str],
) -> None:
    """The doctor dashboard refuses a caller without ``report.doctor.read``."""
    response = await api.get("/api/v1/dashboards/doctor", headers=no_permissions)
    assert response.status_code == 403
    assert response.json()["error_code"] == "PERMISSION_DENIED"


@pytest.mark.parametrize(("method", "path", "permission", "body"), MUTATION_ENDPOINTS)
async def test_mutation_denied_without_permission(
    api: AsyncClient,
    no_permissions: dict[str, str],
    method: str,
    path: str,
    permission: str,
    body: dict[str, Any],
) -> None:
    """Mutating endpoints refuse writes from a caller lacking the permission."""
    response = await api.request(method, path, headers=no_permissions, json=body)
    assert response.status_code in (401, 403), (
        f"{method} {path} accepted an unauthorised write (got {response.status_code})"
    )
    if response.status_code == 403:
        assert response.json()["error_code"] == "PERMISSION_DENIED"
