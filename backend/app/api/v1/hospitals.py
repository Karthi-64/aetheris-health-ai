"""Hospital Settings API routes.

Implements the caller-hospital endpoints of
``docs/modules/14-hospital-settings.md`` §9:

- ``GET  /hospitals/current``       — public/branding fields, any authenticated user
- ``GET  /hospitals/current/feature-flags`` — which gated features are usable,
  any authenticated user
- ``GET  /hospitals/current/full``  — all fields, ``settings.read``
- ``PATCH /hospitals/current``      — editable fields, ``settings.update``

The Superadmin platform endpoints (``/admin/hospitals``, provisioning,
feature-flag toggles) are out of this sprint's scope: there is no platform
panel in the product yet (§12) and no multi-hospital user to administer from.

**Tenancy.** The hospital always comes from the authenticated user, never
from the path or body (CLAUDE.md rule 5).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends

from app.api.dependencies.auth import get_current_user, require_permission
from app.api.dependencies.services import get_hospital_service
from app.core.exceptions import BusinessRuleError
from app.models.user import User
from app.schemas.common import SuccessResponse
from app.schemas.hospital import (
    FeatureFlagsResponse,
    HospitalPublicResponse,
    HospitalSettingsResponse,
    UpdateHospitalSettingsRequest,
)
from app.services.hospital_service import HospitalService

router = APIRouter(prefix="/hospitals", tags=["Hospital Settings"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    404: {"description": "The caller belongs to no active hospital."},
    422: {"description": "Request failed validation."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    A Super Admin has no ``hospital_id`` (``docs/05-DATABASE_DESIGN.md`` §2.2),
    so there is no tenant to read or update — the same rejection
    :mod:`app.api.v1.departments` applies, for the same reason.

    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so settings cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


@router.get(
    "/current",
    response_model=SuccessResponse[HospitalPublicResponse],
    summary="Get my hospital (public fields)",
    description=(
        "Return the caller's hospital with its public and branding fields. "
        "Available to any authenticated user — the shell needs the hospital "
        "name and logo to render."
    ),
    responses={200: {"description": "Hospital returned."}, **_COMMON_RESPONSES},
)
async def get_current_hospital(
    current_user: User = Depends(get_current_user),
    service: HospitalService = Depends(get_hospital_service),
) -> SuccessResponse[HospitalPublicResponse]:
    """Return the caller's hospital, public fields (module spec §9)."""
    hospital = await service.get_current(_tenant_of(current_user))
    data = HospitalPublicResponse.model_validate(hospital)
    return SuccessResponse[HospitalPublicResponse](message="Hospital retrieved.", data=data)


@router.get(
    "/current/feature-flags",
    response_model=SuccessResponse[FeatureFlagsResponse],
    summary="Get my hospital's feature availability",
    description=(
        "Report which gated features the caller's hospital can use right now, as "
        "one boolean per feature. Available to any authenticated user — the "
        "interface needs it to decide whether to show a control.\n\n"
        "`available` is true only when the feature is enabled for the hospital "
        "and the server is configured to serve it. The response does not say "
        "which of the two is missing, and it never contacts an AI provider: "
        "configured is not the same as reachable."
    ),
    responses={200: {"description": "Feature availability returned."}, **_COMMON_RESPONSES},
)
async def get_current_feature_flags(
    current_user: User = Depends(get_current_user),
    service: HospitalService = Depends(get_hospital_service),
) -> SuccessResponse[FeatureFlagsResponse]:
    """Return the caller's hospital feature availability (module spec §9)."""
    data = await service.get_feature_flags(_tenant_of(current_user))
    return SuccessResponse[FeatureFlagsResponse](message="Feature flags retrieved.", data=data)


@router.get(
    "/current/full",
    response_model=SuccessResponse[HospitalSettingsResponse],
    summary="Get my hospital (all editable settings)",
    description="Return every hospital field the settings screen renders. Requires `settings.read`.",
    responses={200: {"description": "Hospital returned."}, **_COMMON_RESPONSES},
)
async def get_current_hospital_full(
    current_user: User = Depends(require_permission("settings.read")),
    service: HospitalService = Depends(get_hospital_service),
) -> SuccessResponse[HospitalSettingsResponse]:
    """Return the full settings record (module spec §9)."""
    hospital = await service.get_current(_tenant_of(current_user))
    data = HospitalSettingsResponse.model_validate(hospital)
    return SuccessResponse[HospitalSettingsResponse](message="Hospital retrieved.", data=data)


@router.patch(
    "/current",
    response_model=SuccessResponse[HospitalSettingsResponse],
    summary="Update my hospital settings",
    description=(
        "Apply a partial update to the editable fields: name, address, phone, "
        "email, locale, logo_url and the settings object. Only fields present "
        "in the body change.\n\n"
        "`slug`, `timezone`, `currency` and `tax_id` are reserved for "
        "Superadmin approval (§9) and are rejected with 422 rather than "
        "silently ignored. The change is recorded in the audit trail with "
        "before/after values."
    ),
    responses={
        200: {"description": "Settings updated."},
        409: {"description": "A duplicate hospital name conflict (not applicable in MVP)."},
        **_COMMON_RESPONSES,
    },
)
async def update_current_hospital(
    payload: UpdateHospitalSettingsRequest,
    current_user: User = Depends(require_permission("settings.update")),
    service: HospitalService = Depends(get_hospital_service),
) -> SuccessResponse[HospitalSettingsResponse]:
    """Update editable hospital settings (module spec §9)."""
    hospital = await service.update_current(
        _tenant_of(current_user),
        payload,
        actor_id=current_user.id,
    )
    data = HospitalSettingsResponse.model_validate(hospital)
    return SuccessResponse[HospitalSettingsResponse](
        message="Hospital settings updated.", data=data
    )
