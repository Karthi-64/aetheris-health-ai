"""API v1 — All version 1 endpoints.

Versioned via URL prefix ``/api/v1``.
See :mod:`app.main` for router registration.
"""

from app.api.v1.appointments import router as appointment_router
from app.api.v1.audit import router as audit_router
from app.api.v1.auth import router as auth_router
from app.api.v1.departments import router as department_router
from app.api.v1.doctors import router as doctor_router
from app.api.v1.health import router as health_router
from app.api.v1.hospitals import router as hospital_router
from app.api.v1.patients import router as patient_router
from app.api.v1.roles import permission_router
from app.api.v1.roles import router as role_router
from app.api.v1.users import router as user_router

__all__ = [
    "appointment_router",
    "audit_router",
    "auth_router",
    "department_router",
    "doctor_router",
    "health_router",
    "hospital_router",
    "patient_router",
    "permission_router",
    "role_router",
    "user_router",
]
