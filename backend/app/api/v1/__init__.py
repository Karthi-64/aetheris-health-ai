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
from app.api.v1.inventory import router as inventory_router
from app.api.v1.invoices import router as invoice_router
from app.api.v1.lab_orders import router as lab_order_router
from app.api.v1.medicines import router as medicine_router
from app.api.v1.notifications import router as notification_router
from app.api.v1.patient import router as patient_app_router
from app.api.v1.patients import router as patient_router
from app.api.v1.prescriptions import router as prescription_router
from app.api.v1.purchase_orders import router as purchase_order_router
from app.api.v1.reports import dashboard_router as dashboard_router
from app.api.v1.reports import router as report_router
from app.api.v1.roles import permission_router
from app.api.v1.roles import router as role_router
from app.api.v1.services import router as service_router
from app.api.v1.tests_catalog import router as tests_catalog_router
from app.api.v1.users import router as user_router
from app.api.v1.vendors import router as vendor_router

__all__ = [
    "appointment_router",
    "audit_router",
    "auth_router",
    "dashboard_router",
    "department_router",
    "doctor_router",
    "health_router",
    "hospital_router",
    "inventory_router",
    "invoice_router",
    "lab_order_router",
    "medicine_router",
    "notification_router",
    "patient_app_router",
    "patient_router",
    "permission_router",
    "prescription_router",
    "purchase_order_router",
    "report_router",
    "role_router",
    "service_router",
    "tests_catalog_router",
    "user_router",
    "vendor_router",
]
