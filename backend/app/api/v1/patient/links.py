"""Patient App record-link routes — link to a record, or register one.

See ``docs/modules/15-patient-app.md`` §4.5 and §27.4.

``hospital_ref`` is the hospital's id or its code (slug). Neither request
carries a phone number or a patient id: the phone is the one the account
proved at sign-in, and the record is found by the server.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Response

from app.api.dependencies.patient import (
    get_client_context,
    get_patient_account,
    get_record_link_service,
)
from app.core.envelope import success_envelope
from app.models.patient_account import PatientAccount
from app.schemas.patient_app.account import LinkRecordRequest, RegisterRecordRequest
from app.services.patient_app.common import ClientContext
from app.services.patient_app.record_link_service import RecordLinkService

router = APIRouter(prefix="/hospitals", tags=["Patient App — Record links"])

HospitalRef = Annotated[
    str, Path(min_length=1, max_length=100, description="The hospital's id or its code.")
]


@router.post(
    "/{hospital_ref}/link",
    status_code=201,
    summary="Link to an existing record",
    description=(
        "Link the signed-in account to its record at a hospital, by date of birth "
        "(and MRN when asked). Never returns any detail of a candidate record."
    ),
    responses={
        200: {"description": "Already linked to that record."},
        201: {"description": "Linked."},
        403: {"description": "Linking is not available in the app (LINK_UNAVAILABLE)."},
        404: {"description": "No record matches these details."},
        409: {"description": "An MRN is needed (LINK_MRN_REQUIRED), or a conflict."},
    },
)
async def link_record(
    hospital_ref: HospitalRef,
    payload: LinkRecordRequest,
    response: Response,
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    link_service: RecordLinkService = Depends(get_record_link_service),
) -> dict[str, Any]:
    """Link the signed-in account to its existing record."""
    outcome = await link_service.link(
        account,
        hospital_ref,
        date_of_birth=payload.date_of_birth,
        mrn=payload.mrn,
        consent_policy_version=payload.consent_policy_version,
        client=client,
    )
    if not outcome.created:
        response.status_code = 200
    return success_envelope(
        "Record linked." if outcome.created else "Record already linked.",
        data=outcome.link.model_dump(mode="json"),
    )


@router.post(
    "/{hospital_ref}/register",
    status_code=201,
    summary="Register a new record",
    description=(
        "Create a record for the signed-in account at a hospital when none matches, "
        "and link it. The record carries the account's verified phone number."
    ),
    responses={
        201: {"description": "Registered and linked."},
        403: {"description": "Registration is not available in the app (LINK_UNAVAILABLE)."},
        404: {"description": "The hospital cannot be used."},
        409: {"description": "A matching record exists, or the account is already linked here."},
    },
)
async def register_record(
    hospital_ref: HospitalRef,
    payload: RegisterRecordRequest,
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    link_service: RecordLinkService = Depends(get_record_link_service),
) -> dict[str, Any]:
    """Register a new record for the signed-in account."""
    result = await link_service.register(account, hospital_ref, payload, client=client)
    return success_envelope("Record registered.", data=result.model_dump(mode="json"))
