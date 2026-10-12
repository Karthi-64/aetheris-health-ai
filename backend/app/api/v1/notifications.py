"""Notification API routes.

Implements the MVP endpoints in ``docs/modules/11-notifications.md`` §9.
Routes parse input, delegate to
:class:`~app.services.notification_service.NotificationService`, and wrap the
result in the standard envelope. No business logic, no database access.

**Everything here is about the caller's own notifications.** No endpoint takes
a user id: the recipient is always the authenticated user, so there is nothing
to get wrong about whose notifications are being read.

**Not here yet.** The template and delivery-log endpoints in §9 are v2.1;
calling them is a 404.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Path, Query

from app.api.dependencies.auth import require_permission
from app.api.dependencies.services import get_notification_service
from app.core.exceptions import BusinessRuleError
from app.models.user import User
from app.schemas.common import (
    MetadataWithPagination,
    PaginatedResponse,
    PaginationMeta,
    PaginationParams,
    SuccessResponse,
)
from app.schemas.notification import (
    BroadcastRequest,
    BroadcastResponse,
    NotificationPreferencesResponse,
    NotificationResponse,
    ReadAllResponse,
    UnreadCountResponse,
    UpdatePreferencesRequest,
)
from app.services.notification_service import NotificationService

router = APIRouter(prefix="/notifications", tags=["Notifications"])

_COMMON_RESPONSES: dict[int | str, dict[str, str]] = {
    401: {"description": "Missing or invalid access token."},
    403: {"description": "Authenticated but lacking the required permission."},
    422: {"description": "Request failed validation."},
}

_READ_OWN = "notification.read.own"
_PREFERENCES = "notification.preference.update.own"


def _tenant_of(current_user: User) -> uuid.UUID:
    """Return the hospital the request acts within.

    :param current_user: The authenticated user.
    :returns: The hospital UUID to scope every query by.
    :raises BusinessRuleError: If the user belongs to no hospital.
    """
    if current_user.hospital_id is None:
        msg = "This account is not scoped to a hospital, so it has no notifications."
        raise BusinessRuleError(msg)
    return current_user.hospital_id


# ── Notification centre ─────────────────────────────────────────────────────


@router.get(
    "",
    response_model=PaginatedResponse[NotificationResponse],
    summary="List my notifications",
    description=(
        "Return a page of the caller's notifications, newest first. Pass "
        "`unread_only=true` for just the unread ones."
    ),
    responses={200: {"description": "Page of notifications returned."}, **_COMMON_RESPONSES},
)
async def list_notifications(
    unread_only: bool = Query(False, description="Only notifications not yet read."),
    page: int = Query(1, ge=1, description="1-based page number."),
    page_size: int = Query(25, ge=1, le=100, description="Records per page."),
    current_user: User = Depends(require_permission(_READ_OWN)),
    service: NotificationService = Depends(get_notification_service),
) -> PaginatedResponse[NotificationResponse]:
    """List the caller's notifications."""
    page_result = await service.list_mine(
        _tenant_of(current_user),
        current_user.id,
        pagination=PaginationParams(page=page, page_size=page_size),
        unread_only=unread_only,
    )
    return PaginatedResponse[NotificationResponse](
        message="Notifications retrieved.",
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
    "/unread-count",
    response_model=SuccessResponse[UnreadCountResponse],
    summary="Count my unread notifications",
    description="Return the number to show on the notification bell.",
    responses={200: {"description": "Count returned."}, **_COMMON_RESPONSES},
)
async def unread_count(
    current_user: User = Depends(require_permission(_READ_OWN)),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[UnreadCountResponse]:
    """Return the caller's unread count."""
    count = await service.unread_count(_tenant_of(current_user), current_user.id)
    return SuccessResponse[UnreadCountResponse](message="Unread count retrieved.", data=count)


@router.post(
    "/read-all",
    response_model=SuccessResponse[ReadAllResponse],
    summary="Mark all my notifications read",
    description="Mark every unread notification of the caller as read.",
    responses={200: {"description": "Notifications marked read."}, **_COMMON_RESPONSES},
)
async def mark_all_read(
    current_user: User = Depends(require_permission(_READ_OWN)),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[ReadAllResponse]:
    """Mark all of the caller's notifications read."""
    result = await service.mark_all_read(_tenant_of(current_user), current_user.id)
    return SuccessResponse[ReadAllResponse](message="Notifications marked read.", data=result)


@router.post(
    "/{notification_id}/read",
    response_model=SuccessResponse[NotificationResponse],
    summary="Mark a notification read",
    description=(
        "Mark one of the caller's notifications read. Marking one that is "
        "already read is not an error. A notification addressed to someone "
        "else is a `404`."
    ),
    responses={
        200: {"description": "Notification marked read."},
        404: {"description": "Notification not found among the caller's own."},
        **_COMMON_RESPONSES,
    },
)
async def mark_read(
    notification_id: uuid.UUID = Path(description="Notification UUID."),
    current_user: User = Depends(require_permission(_READ_OWN)),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[NotificationResponse]:
    """Mark one of the caller's notifications read."""
    notification = await service.mark_read(
        _tenant_of(current_user), current_user.id, notification_id
    )
    return SuccessResponse[NotificationResponse](
        message="Notification marked read.", data=notification
    )


# ── Preferences ─────────────────────────────────────────────────────────────


@router.get(
    "/preferences",
    response_model=SuccessResponse[NotificationPreferencesResponse],
    summary="Get my notification preferences",
    description=(
        "Return every notification kind with the channels currently in effect "
        "for the caller, and which of them cannot be switched off."
    ),
    responses={200: {"description": "Preferences returned."}, **_COMMON_RESPONSES},
)
async def get_preferences(
    current_user: User = Depends(require_permission(_READ_OWN)),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[NotificationPreferencesResponse]:
    """Return the caller's preferences."""
    _tenant_of(current_user)
    preferences = await service.get_preferences(current_user.id)
    return SuccessResponse[NotificationPreferencesResponse](
        message="Preferences retrieved.", data=preferences
    )


@router.put(
    "/preferences",
    response_model=SuccessResponse[NotificationPreferencesResponse],
    summary="Update my notification preferences",
    description=(
        "Set which channels the caller wants for each kind. Kinds not "
        "mentioned are left as they were.\n\n"
        "A **critical** kind (account security) is always delivered on its "
        "default channels: a preference to switch one off is stored but has "
        "no effect, and the response shows what is actually in effect."
    ),
    responses={200: {"description": "Preferences updated."}, **_COMMON_RESPONSES},
)
async def update_preferences(
    payload: UpdatePreferencesRequest,
    current_user: User = Depends(require_permission(_PREFERENCES)),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[NotificationPreferencesResponse]:
    """Update the caller's preferences."""
    preferences = await service.update_preferences(
        _tenant_of(current_user), current_user.id, payload
    )
    return SuccessResponse[NotificationPreferencesResponse](
        message="Preferences updated.", data=preferences
    )


# ── Broadcast ───────────────────────────────────────────────────────────────


@router.post(
    "/broadcast",
    response_model=SuccessResponse[BroadcastResponse],
    summary="Broadcast an announcement",
    description=(
        "Send an announcement to every active user in the hospital, or to "
        "those holding `role_id`. Each recipient gets their own notification. "
        "`link`, if given, must be an in-app path."
    ),
    responses={200: {"description": "Announcement sent."}, **_COMMON_RESPONSES},
)
async def broadcast(
    payload: BroadcastRequest,
    current_user: User = Depends(require_permission("notification.broadcast")),
    service: NotificationService = Depends(get_notification_service),
) -> SuccessResponse[BroadcastResponse]:
    """Broadcast an announcement to a role or to the hospital."""
    result = await service.broadcast(_tenant_of(current_user), payload, actor_id=current_user.id)
    return SuccessResponse[BroadcastResponse](message="Announcement sent.", data=result)
