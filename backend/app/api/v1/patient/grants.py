"""Patient App access-grant routes — create, list, revoke (§8.3, §27.5).

No route names a patient or a source hospital for ownership: what a grant
shares is the caller's own record at the hospital in the creation path,
resolved by the service. The authorization gate (``authorize()``) is not an
endpoint; the recipient side has no HTTP caller yet (§8.3.3).
"""

from __future__ import annotations

from typing import Annotated, Any, Final
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.patient import (
    get_access_grant_service,
    get_client_context,
    get_patient_account,
    get_patient_hospital_gate,
)
from app.core.envelope import paginated_envelope, success_envelope
from app.core.exceptions import BusinessRuleError, NotFoundError, ValidationError
from app.models.patient_account import PatientAccount
from app.models.patient_consent import GrantStatus
from app.schemas.common import PaginatedResponse, SuccessResponse
from app.schemas.patient_app.grants import (
    AccessGrantView,
    CreateAccessGrant,
    RevokeAccessGrant,
)
from app.schemas.patient_app.hospitals import MAX_PAGE, MAX_PAGE_SIZE
from app.services.patient_app.access_grant_service import (
    DEFAULT_GRANT_PAGE_SIZE,
    AccessGrantService,
)
from app.services.patient_app.common import ClientContext
from app.services.patient_app.hospital_gate import PatientHospitalGate

router = APIRouter(tags=["Patient App — Access grants"])

HospitalRef = Annotated[
    str, Path(min_length=1, max_length=100, description="The hospital's id or its code.")
]
NotFound: Final[dict[int | str, dict[str, str]]] = {
    404: {"description": "No such hospital, grant, or recipient."}
}
Conflict: Final[dict[int | str, dict[str, str]]] = {
    409: {"description": "The grant has already been revoked."}
}


@router.post(
    "/hospitals/{hospital_ref}/access-grants",
    status_code=201,
    response_model=SuccessResponse[AccessGrantView],
    summary="Create an access grant",
    description=(
        "Let a hospital, or one doctor at one hospital, read named categories "
        "of your record at the hospital in the path. Thirty days by default, "
        "never beyond 365. `documents` cannot be shared: no document source "
        "exists yet."
    ),
    responses={
        201: {"description": "Grant created."},
        400: {"description": "The grant is not valid (BUSINESS_RULE_VIOLATION)."},
        401: {"description": "Authentication required."},
        403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
        422: {"description": "A malformed body."},
        **NotFound,
    },
)
async def create_access_grant(
    hospital_ref: HospitalRef,
    payload: CreateAccessGrant,
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    gate: PatientHospitalGate = Depends(get_patient_hospital_gate),
    grants: AccessGrantService = Depends(get_access_grant_service),
) -> dict[str, Any]:
    """Let a recipient read categories of the caller's own record."""
    hospital = await gate.resolve(hospital_ref)
    if hospital is None:
        raise NotFoundError("Not Found")
    try:
        grant = await grants.create(
            account,
            hospital.id,
            grantee_type=payload.grantee_type,
            grantee_id=payload.grantee_id,
            grantee_hospital_id=payload.grantee_hospital_id,
            categories=payload.categories,
            purpose_note=payload.purpose_note,
            expires_at=payload.expires_at,
            records_from=payload.records_from,
            records_to=payload.records_to,
            client=client,
        )
    except ValidationError as error:
        # §27.5: an invalid grant — an unknown recipient, a bad expiry, the
        # documents category — is a 400 BUSINESS_RULE_VIOLATION, not a 422.
        raise BusinessRuleError(message=error.message, detail=error.detail) from error
    return success_envelope("Access grant created.", data=grant.model_dump(mode="json"))


@router.get(
    "/access-grants",
    response_model=PaginatedResponse[AccessGrantView],
    summary="List my access grants",
    description=(
        "Every grant you have given, across the hospitals where your record is "
        "honoured, newest first, each with its status derived as of now."
    ),
    responses={
        200: {"description": "Page of grants returned."},
        401: {"description": "Authentication required."},
        403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
    },
)
async def list_access_grants(
    grant_status: GrantStatus | None = Query(
        None, alias="status", description="Only grants in this state."
    ),
    page: int = Query(1, ge=1, le=MAX_PAGE, description="1-based page number."),
    page_size: int = Query(
        DEFAULT_GRANT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Grants per page."
    ),
    account: PatientAccount = Depends(get_patient_account),
    grants: AccessGrantService = Depends(get_access_grant_service),
) -> dict[str, Any]:
    """List the grants the caller has given."""
    result = await grants.list_grants(account, status=grant_status, page=page, page_size=page_size)
    return paginated_envelope(
        "Access grants retrieved.",
        data=[item.model_dump(mode="json") for item in result.items],
        page=result.page,
        page_size=result.page_size,
        total_records=result.total_records,
    )


@router.post(
    "/access-grants/{grant_id}/revoke",
    response_model=SuccessResponse[AccessGrantView],
    summary="Revoke an access grant",
    description=(
        "Take back a grant you gave, whatever has happened to your link since. "
        "It stops working on the recipient's next attempt — no grace period."
    ),
    responses={
        200: {"description": "Grant revoked."},
        401: {"description": "Authentication required."},
        403: {"description": "A required policy has not been accepted (CONSENT_REQUIRED)."},
        404: {"description": "No such grant was given by this account."},
        409: {"description": "The grant has already been revoked."},
        422: {"description": "A malformed body."},
    },
)
async def revoke_access_grant(
    grant_id: UUID = Path(description="The grant's id."),
    payload: RevokeAccessGrant | None = None,
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    grants: AccessGrantService = Depends(get_access_grant_service),
) -> dict[str, Any]:
    """Take back one of the caller's grants."""
    grant = await grants.revoke(
        account,
        None,
        grant_id,
        reason=payload.reason if payload is not None else None,
        client=client,
        repeat_conflict=True,
    )
    return success_envelope("Access grant revoked.", data=grant.model_dump(mode="json"))
