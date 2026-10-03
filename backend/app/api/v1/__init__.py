"""API v1 — All version 1 endpoints.

Versioned via URL prefix ``/api/v1``.
See :mod:`app.main` for router registration.
"""

from app.api.v1.appointments import router as appointment_router
from app.api.v1.auth import router as auth_router
from app.api.v1.departments import router as department_router
from app.api.v1.doctors import router as doctor_router
from app.api.v1.health import router as health_router
from app.api.v1.invoices import router as invoice_router
from app.api.v1.patients import router as patient_router
from app.api.v1.roles import permission_router
from app.api.v1.roles import router as role_router
from app.api.v1.services import router as service_router
from app.api.v1.users import router as user_router

__all__ = [
    "appointment_router",
    "auth_router",
    "department_router",
    "doctor_router",
    "health_router",
    "invoice_router",
    "patient_router",
    "permission_router",
    "role_router",
    "service_router",
    "user_router",
]
