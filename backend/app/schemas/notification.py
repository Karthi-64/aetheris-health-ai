"""Pydantic DTOs for the Notifications module.

Request models enforce ``docs/modules/11-notifications.md`` before a service
sees the payload. Response models are the only notification shapes that cross
the API boundary.
"""

from __future__ import annotations

# NOTE: runtime imports, not TYPE_CHECKING — Pydantic resolves field
# annotations against the module's real globals (backend/CLAUDE.md).
from datetime import datetime  # noqa: TC003
from typing import TYPE_CHECKING, Self
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import Page

if TYPE_CHECKING:
    from app.models.notification import Notification

__all__ = [
    "BroadcastRequest",
    "BroadcastResponse",
    "ChannelPreference",
    "KindPreferenceResponse",
    "NotificationListResponse",
    "NotificationPreferencesResponse",
    "NotificationResponse",
    "ReadAllResponse",
    "UnreadCountResponse",
    "UpdatePreferencesRequest",
]


def _strip_required(value: str, label: str) -> str:
    """Trim a required string and reject one that is blank."""
    stripped = value.strip()
    if not stripped:
        msg = f"{label} must not be blank."
        raise ValueError(msg)
    return stripped


# ── Notification centre ─────────────────────────────────────────────────────


class NotificationResponse(BaseModel):
    """One entry in the notification centre."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Notification UUID.")
    kind: str = Field(description="Kind code, e.g. 'billing.discount_approval_requested'.")
    title: str = Field(description="Short headline.")
    body: str = Field(description="Message text.")
    link: str | None = Field(description="In-app path to open, if any.")
    is_read: bool = Field(description="Whether the recipient has read it.")
    read_at: datetime | None = Field(description="When it was read (UTC).")
    created_at: datetime = Field(description="When it was raised (UTC).")

    @classmethod
    def from_model(cls, notification: Notification) -> Self:
        """Build a DTO from an ORM instance.

        :param notification: The ORM instance to convert.
        :returns: The populated DTO.
        """
        return cls.model_validate(notification)


class UnreadCountResponse(BaseModel):
    """The number on the bell (FR-1)."""

    unread: int = Field(ge=0, description="Notifications not yet read.")


class ReadAllResponse(BaseModel):
    """Result of ``POST /notifications/read-all``."""

    marked: int = Field(ge=0, description="How many notifications were marked read.")


# ── Preferences ─────────────────────────────────────────────────────────────


class ChannelPreference(BaseModel):
    """A user's choice of channels for one kind.

    A channel left out is left as it is. ``sms`` is not accepted: SMS is v2.1.
    """

    model_config = ConfigDict(extra="forbid")

    in_app: bool | None = Field(default=None, description="Show in the notification centre.")
    email: bool | None = Field(default=None, description="Also send by email.")


class UpdatePreferencesRequest(BaseModel):
    """Payload for ``PUT /api/v1/notifications/preferences`` (FR-3).

    ``preferences`` maps a kind code to the channels wanted for it. Kinds not
    mentioned keep whatever was stored for them.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "preferences": {
                    "billing.discount_approval_requested": {"email": True},
                    "system.broadcast": {"in_app": False},
                }
            },
        },
    )

    preferences: dict[str, ChannelPreference] = Field(
        description="Kind code to the channels wanted for it."
    )


class KindPreferenceResponse(BaseModel):
    """One kind on the preferences page, with what is in effect for the user."""

    kind: str = Field(description="Kind code.")
    category: str = Field(description="Grouping for the preferences page.")
    label: str = Field(description="Human-readable name.")
    critical: bool = Field(
        description="A critical kind is always delivered on its default channels."
    )
    in_app: bool = Field(description="Whether it is shown in the notification centre.")
    email: bool = Field(description="Whether it is also sent by email.")
    email_available: bool = Field(description="Whether this kind has an email form at all.")
    locked_channels: list[str] = Field(
        description="Channels the user cannot switch off for this kind."
    )


class NotificationPreferencesResponse(BaseModel):
    """Body of ``GET`` and ``PUT /notifications/preferences``."""

    kinds: list[KindPreferenceResponse] = Field(description="Every kind, in page order.")


# ── Broadcast ───────────────────────────────────────────────────────────────


class BroadcastRequest(BaseModel):
    """Payload for ``POST /api/v1/notifications/broadcast`` (FR-6).

    Sends one announcement to every active user in the hospital, or to those
    holding ``role_id``.
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "example": {
                "title": "Scheduled maintenance tonight",
                "body": "The system will be unavailable from 23:00 to 23:30.",
            },
        },
    )

    title: str = Field(min_length=1, max_length=200, description="Headline.")
    body: str = Field(min_length=1, max_length=2000, description="Message text.")
    link: str | None = Field(default=None, max_length=500, description="In-app path to open.")
    role_id: UUID | None = Field(
        default=None, description="Only users holding this role. Omit for the whole hospital."
    )

    @field_validator("title")
    @classmethod
    def _check_title(cls, value: str) -> str:
        """Trim the title and reject a blank one."""
        return _strip_required(value, "Title")

    @field_validator("body")
    @classmethod
    def _check_body(cls, value: str) -> str:
        """Trim the body and reject a blank one."""
        return _strip_required(value, "Body")

    @field_validator("link")
    @classmethod
    def _check_link(cls, value: str | None) -> str | None:
        """Allow only an in-app path as a link.

        A broadcast reaches every user, so an absolute URL here would be a
        ready-made phishing link with the hospital's name on it.
        """
        if value is None:
            return None
        link = value.strip()
        if not link:
            return None
        if not link.startswith("/") or link.startswith("//"):
            msg = "Link must be an in-app path starting with a single '/'."
            raise ValueError(msg)
        return link


class BroadcastResponse(BaseModel):
    """Result of a broadcast."""

    recipients: int = Field(ge=0, description="How many users were notified.")


#: One page of notifications — the body of a list response.
NotificationListResponse = Page[NotificationResponse]
