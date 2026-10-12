"""Lab test catalog API routes.

Implements the catalog half of ``docs/modules/07-laboratory.md`` §9. Routes
parse input, delegate to
:class:`~app.services.lab_catalog_service.LabCatalogService`, and wrap the
result in the standard envelope. No business logic, no database access.

**Tenancy.** ``hospital_id`` always comes from the authenticated user.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query, status

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_lab_catalog_service
from app.core.exceptions import BusinessRuleError
from app.models.user import User
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.schemas.lab import CreateLabTestRequest, LabTestResponse, UpdateLabTestRequest
from app.services.lab_catalog_service import LabCatalogService

router = APIRouter(prefix="/tests-catalog", tags=["Laboratory — Test catalog"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_NOT_FOUND_RESPONSE: dict[int | str, dict[str, str]] = {
    404: {"description": "Lab test not found in this hospital."},
}


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so the test catalog cannot be accessed."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=SuccessResponse[LabTestResponse],
    summary="Add a test to the catalog",
    description=(
        "Create an orderable lab test in the caller's hospital.\n\n"
        "`code` is uppercased automatically and must be unique within the "
        "hospital. A `numeric` test needs at least one reference range; a "
        "`text` test cannot have any."
    ),
    responses={
        201: {"description": "Test created."},
        409: {"description": "A test with this code already exists."},
        **_COMMON_RESPONSES,
    },
)
async def create_test(
    payload: CreateLabTestRequest,
    current_user: User = Depends(require_permission("lab.test.create")),
    service: LabCatalogService = Depends(get_lab_catalog_service),
) -> SuccessResponse[LabTestResponse]:
    """Create a catalog test (module spec §9)."""
    created = await service.create_test(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[LabTestResponse](message="Lab test created successfully.", data=created)


@router.get(
    "",
    response_model=PaginatedResponse[LabTestResponse],
    summary="List the test catalog",
    description=(
        "Return a page of catalog tests, ordered by name.\n\n"
        "`q` matches a name prefix case-insensitively, or an exact code. "
        "`is_active` filters on whether a test can still be ordered; omit it "
        "to get both."
    ),
    responses={200: {"description": "Page of tests returned."}, **_COMMON_RESPONSES},
)
async def list_tests(
    q: str | None = Query(None, max_length=200, description="Name prefix or exact code."),
    category: str | None = Query(None, max_length=100, description="Exact category."),
    is_active: bool | None = Query(None, description="Filter on the active flag."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission("lab.test.read")),
    service: LabCatalogService = Depends(get_lab_catalog_service),
) -> PaginatedResponse[LabTestResponse]:
    """List catalog tests (module spec §9)."""
    page_result = await service.list_tests(
        _tenant_of(current_user),
        pagination=PaginationParams(page=page, page_size=page_size),
        term=q,
        category=category,
        is_active=is_active,
    )
    return PaginatedResponse[LabTestResponse](
        message="Lab tests retrieved.",
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
    "/{test_id}",
    response_model=SuccessResponse[LabTestResponse],
    summary="Get a catalog test",
    description="Return one catalog test with its reference ranges.",
    responses={
        200: {"description": "Test returned."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def get_test(
    test_id: uuid.UUID = Path(description="Lab test UUID."),
    current_user: User = Depends(require_permission("lab.test.read")),
    service: LabCatalogService = Depends(get_lab_catalog_service),
) -> SuccessResponse[LabTestResponse]:
    """Retrieve one catalog test."""
    test = await service.get_test(_tenant_of(current_user), test_id)
    return SuccessResponse[LabTestResponse](message="Lab test retrieved.", data=test)


@router.patch(
    "/{test_id}",
    response_model=SuccessResponse[LabTestResponse],
    summary="Update a catalog test",
    description=(
        "Apply a partial update. `code` and `result_type` cannot be changed. "
        "`reference_ranges`, if sent, replaces the whole list. Retire a test "
        "with `is_active: false`. Orders already placed are not affected."
    ),
    responses={
        200: {"description": "Test updated."},
        **_NOT_FOUND_RESPONSE,
        **_COMMON_RESPONSES,
    },
)
async def update_test(
    payload: UpdateLabTestRequest,
    test_id: uuid.UUID = Path(description="Lab test UUID."),
    current_user: User = Depends(require_permission("lab.test.update")),
    service: LabCatalogService = Depends(get_lab_catalog_service),
) -> SuccessResponse[LabTestResponse]:
    """Update a catalog test."""
    updated = await service.update_test(
        _tenant_of(current_user), test_id, payload, actor_id=current_user.id
    )
    return SuccessResponse[LabTestResponse](message="Lab test updated successfully.", data=updated)
