"""Services catalog API routes.

Implements the catalog half of ``docs/modules/06-billing.md`` §9. Routes parse
input, delegate to
:class:`~app.services.service_catalog_service.ServiceCatalogService`, and wrap
the result in the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.

**Naming.** "Service" here is the billing domain's word for a billable item —
a consultation, a dressing, a lab panel — and has nothing to do with the
business-logic layer of the same name.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_service_catalog_service
from app.core.exceptions import BusinessRuleError
from app.models.user import User
from app.schemas.billing import CreateServiceRequest, ServiceResponse, UpdateServiceRequest
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.services.service_catalog_service import ServiceCatalogService

router = APIRouter(prefix="/services", tags=["Billing — Services catalog"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, str]] = {
    404: {"description": "Service not found in this hospital."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    A Super Admin has no ``hospital_id``, so there is no tenant to scope the
    catalog to. Rejected rather than silently querying across tenants.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = (
            "This account is not scoped to a hospital, so the services catalog cannot be accessed."
        )
        raise BusinessRuleError(msg)
    return current_user.hospital_id


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[ServiceResponse],
    summary="Add a service to the catalog",
    description=(
        "Create a billable service in the caller's hospital.\n\n"
        "`code` is uppercased automatically and must be unique within the "
        "hospital. `price` is a decimal string in the hospital's currency."
    ),
    responses={
        201: {"description": "Service created."},
        409: {"description": "A service with this code already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_service(
    payload: CreateServiceRequest,
    current_user: User = Depends(require_permission("service.create")),
    service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> SuccessResponse[ServiceResponse]:
    """Create a catalog service (module spec §9)."""
    created = await service.create_service(
        _tenant_of(current_user), payload, actor_id=current_user.id
    )
    return SuccessResponse[ServiceResponse](message="Service created successfully.", data=created)


@router.get(
    "",
    response_model=PaginatedResponse[ServiceResponse],
    summary="List the services catalog",
    description=(
        "Return a page of catalog services, ordered by name.\n\n"
        "`q` matches a name prefix case-insensitively, or an exact code. "
        "`is_active` filters on whether a service can still be billed; omit it "
        "to get both."
    ),
    responses={200: {"description": "Page of services returned."}, **_COMMON_RESPONSES},
)
async def list_services(
    q: str | None = Query(None, max_length=200, description="Name prefix or exact code."),
    category: str | None = Query(None, max_length=100, description="Exact category."),
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("service.read")),
    service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> PaginatedResponse[ServiceResponse]:
    """List catalog services (module spec §9)."""
    page_result = await service.list_services(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        term=q,
        category=category,
        is_active=is_active,
    )
    return PaginatedResponse[ServiceResponse](
        message="Services retrieved.",
        data=page_result.items,
        metadata=MetadataWithPagination(
            pagination=PaginationMeta(
                page=page_result.page,
                page_size=page_result.page_size,
                total_records=page_result.total_records,
                total_pages=page_result.total_pages,
            ),
        ),
    )


@router.get(
    "/{service_id}",
    response_model=SuccessResponse[ServiceResponse],
    summary="Get a catalog service",
    description="Return one catalog service.",
    responses={
        200: {"description": "Service returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_service(
    service_id: uuid.UUID = Path(description="Service UUID."),
    current_user: User = Depends(require_permission("service.read")),
    service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> SuccessResponse[ServiceResponse]:
    """Retrieve one catalog service by UUID."""
    found = await service.get_service(_tenant_of(current_user), service_id)
    return SuccessResponse[ServiceResponse](message="Service retrieved.", data=found)


@router.patch(
    "/{service_id}",
    response_model=SuccessResponse[ServiceResponse],
    summary="Update a catalog service",
    description=(
        "Apply a partial update. Only fields present in the request body are "
        "changed.\n\n"
        "`code` is immutable and is rejected if supplied. Changing `price` "
        "affects future invoice lines only — lines already written keep the "
        "price they were billed at. Set `is_active` to `false` to retire a "
        "service; there is no delete."
    ),
    responses={
        200: {"description": "Service updated."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def update_service(
    payload: UpdateServiceRequest,
    service_id: uuid.UUID = Path(description="Service UUID."),
    current_user: User = Depends(require_permission("service.update")),
    service: ServiceCatalogService = Depends(get_service_catalog_service),
) -> SuccessResponse[ServiceResponse]:
    """Update a catalog service (module spec §9)."""
    updated = await service.update_service(
        _tenant_of(current_user), service_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[ServiceResponse](message="Service updated successfully.", data=updated)
