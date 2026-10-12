"""Patient App API — every endpoint under ``/api/v1/patient``.

A namespace of its own for a principal of its own
(``docs/modules/15-patient-app.md`` §27). Nothing in this package imports the
staff authentication dependencies: a patient endpoint is public, is
authenticated by the patient refresh cookie, or depends on
:func:`app.api.dependencies.patient.get_patient_account`.

See :mod:`app.main` for registration.
"""

from fastapi import APIRouter

from app.api.v1.patient.appointments import router as appointments_router
from app.api.v1.patient.auth import router as auth_router
from app.api.v1.patient.availability import router as availability_router
from app.api.v1.patient.doctors import router as doctors_router
from app.api.v1.patient.hospitals import router as hospitals_router
from app.api.v1.patient.links import router as links_router
from app.api.v1.patient.me import router as me_router
from app.api.v1.patient.my_appointments import router as my_appointments_router

router = APIRouter(prefix="/patient")
router.include_router(auth_router)
router.include_router(me_router)
router.include_router(hospitals_router)
router.include_router(doctors_router)
router.include_router(availability_router)
router.include_router(appointments_router)
router.include_router(my_appointments_router)
router.include_router(links_router)

__all__ = ["router"]
