"""Pydantic DTOs for the Hospital Settings module.

Request models validate everything listed in
``docs/modules/14-hospital-settings.md`` §11 before a service sees it
(``docs/07-SECURITY.md`` rule 5). The update model forbids unknown fields, so
the restricted columns the spec reserves for Superadmin approval — ``slug``,
``timezone``, ``currency``, ``tax_id`` (§9) — are rejected with a 422 rather
than silently ignored: the caller learns the write did not happen.
"""

from __future__ import annotations

# NOTE: ``datetime``/``UUID`` must be imported at runtime, not under
# TYPE_CHECKING — Pydantic resolves annotations against the module globals.
from datetime import datetime  # noqa: TC003
from typing import Any, Self  # noqa: TC003
from uuid import UUID  # noqa: TC003

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "HospitalPublicResponse",
    "HospitalSettingsResponse",
    "UpdateHospitalSettingsRequest",
]

#: Update fields whose columns are ``NOT NULL``. Declared optional so a PATCH
#: can omit them, but an explicit ``null`` is rejected — see
#: :meth:`UpdateHospitalSettingsRequest._reject_null_for_non_nullable_columns`.
#: ``phone``, ``email`` and ``logo_url`` are intentionally absent: their columns
#: are nullable, so ``null`` is the supported way to clear them.
_NON_NULLABLE_UPDATE_FIELDS = frozenset({"name", "address", "locale", "settings"})

#: Keys in ``hospitals.settings`` that are feature flags, e.g.
#: ``feature.ai.slot_recommendation``. Read-only to a hospital admin.
_FEATURE_FLAG_PREFIX = "feature."

#: Pragmatic RFC 5322 subset, identical to the one in
#: :mod:`app.schemas.department` (no new dependencies — CLAUDE.md).
_EMAIL_PATTERN = r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$"

#: Permissive phone shape: digits with optional spaces, dashes and a leading +.
_PHONE_PATTERN = r"^\+?[0-9][0-9 -]{4,19}$"


class HospitalPublicResponse(BaseModel):
    """Branding/public fields any authenticated user may read (§9)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(description="Hospital UUID.")
    name: str = Field(description="Full hospital name.")
    slug: str = Field(description="URL-friendly identifier.")
    address: dict[str, Any] = Field(description="Structured address.")
    phone: str | None = Field(default=None, description="Primary contact phone.")
    email: str | None = Field(default=None, description="Primary contact email.")
    logo_url: str | None = Field(default=None, description="Logo URL.")
    timezone: str = Field(description="IANA timezone.")
    currency: str = Field(description="ISO 4217 currency code.")
    locale: str = Field(description="Locale for formatting.")
    is_active: bool = Field(description="Whether the hospital is active.")


class HospitalSettingsResponse(HospitalPublicResponse):
    """Everything ``GET /hospitals/current/full`` returns (settings.read)."""

    tax_id: str | None = Field(default=None, description="Tax / registration ID.")
    settings: dict[str, Any] = Field(
        default_factory=dict, description="Working hours, policies, feature flags."
    )
    created_at: datetime = Field(description="Creation timestamp.")
    updated_at: datetime = Field(description="Last update timestamp.")


class UpdateHospitalSettingsRequest(BaseModel):
    """Editable hospital fields for ``PATCH /hospitals/current`` (§9).

    ``extra="forbid"`` is load-bearing: restricted fields (``slug``,
    ``timezone``, ``currency``, ``tax_id``) and typos produce a 422 instead of
    being dropped, so an admin never believes a save landed when it did not.
    """

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=2, max_length=200)
    address: dict[str, Any] | None = Field(
        default=None, description="Structured address (street, city, state, zip, country)."
    )
    phone: str | None = Field(default=None, max_length=20, pattern=_PHONE_PATTERN)
    email: str | None = Field(default=None, max_length=200, pattern=_EMAIL_PATTERN)
    locale: str | None = Field(
        default=None, max_length=10, pattern=r"^[a-z]{2}(-[A-Za-z0-9]{2,8})*$"
    )
    logo_url: str | None = Field(default=None, max_length=500)
    settings: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Working hours / policies. **Merged** into the stored object key by "
            "key: keys not sent are kept, and a key sent as null is removed. "
            "Feature flags (`feature.*`) cannot be set here."
        ),
    )

    @field_validator("address")
    @classmethod
    def _address_values_are_scalars(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """The address is a flat string map — no nested objects (§2.3)."""
        if value is None:
            return None
        if len(value) > 20:
            msg = "Address has too many fields."
            raise ValueError(msg)
        for key, item in value.items():
            if not isinstance(item, str):
                msg = f"Address field '{key}' must be a string."
                raise ValueError(msg)
            if len(item) > 300:
                msg = f"Address field '{key}' is too long."
                raise ValueError(msg)
        return value

    @field_validator("settings")
    @classmethod
    def _no_feature_flags(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        """Refuse feature flags in a hospital admin's settings update.

        Module spec §4 rule 8 and §10: flags are toggled only by the platform
        Superadmin (``platform.feature_flags.toggle``), and the Settings page
        shows them read-only to a hospital admin. They live in the same JSONB
        object as the editable policies, so without this check an admin could
        switch on a gated feature for their own hospital with one PATCH.
        """
        if value is None:
            return None
        flags = sorted(key for key in value if key.startswith(_FEATURE_FLAG_PREFIX))
        if flags:
            msg = (
                f"Feature flags can only be changed by a platform administrator: "
                f"{', '.join(flags)}."
            )
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _reject_null_for_non_nullable_columns(self) -> Self:
        """Reject an explicit ``null`` on a column the database requires.

        Every field here is ``| None`` so that it can be *omitted*, but ``name``,
        ``address``, ``locale`` and ``settings`` back ``NOT NULL`` columns.
        Because the service uses ``model_dump(exclude_unset=True)`` — which keeps
        explicit nulls — ``PATCH {"name": null}`` used to reach the database and
        return a 500 IntegrityError instead of a 422 naming the field (PR #29
        review finding 4; same pattern as ``app.schemas.patient``).
        """
        offenders = sorted(
            name
            for name in _NON_NULLABLE_UPDATE_FIELDS & self.model_fields_set
            if getattr(self, name) is None
        )
        if offenders:
            msg = f"These fields cannot be set to null: {', '.join(offenders)}."
            raise ValueError(msg)
        return self
