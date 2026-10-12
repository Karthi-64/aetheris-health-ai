"""Patient App account route — ``GET /patient/me``.

See ``docs/modules/15-patient-app.md`` §27.3.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies.patient import get_patient_account, get_patient_account_service
from app.core.envelope import success_envelope
from app.models.patient_account import PatientAccount
from app.services.patient_app.patient_account_service import PatientAccountService

router = APIRouter(tags=["Patient App — Account"])


@router.get(
    "/me",
    summary="The signed-in patient",
    description="The account (phone masked), its record links and the policies still to accept.",
    responses={
        200: {"description": "The signed-in account."},
        401: {"description": "Authentication required."},
    },
)
async def get_me(
    account: PatientAccount = Depends(get_patient_account),
    account_service: PatientAccountService = Depends(get_patient_account_service),
) -> dict[str, Any]:
    """Describe the signed-in patient account."""
    result = await account_service.get_me(account)
    return success_envelope("Account retrieved.", data=result.model_dump(mode="json"))
