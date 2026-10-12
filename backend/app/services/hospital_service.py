"""Hospital settings service — reads and updates the caller's hospital.

Implements the Hospital Admin surface of ``docs/modules/14-hospital-settings.md``
§9. Tenancy is always passed in from the authenticated user; the service never
reads a hospital id from a request body (CLAUDE.md rule 5).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING

from app.core.audit import AuditEvent, AuditSink
from app.core.exceptions import NotFoundError
from app.core.feature_flags import AI_SLOT_RECOMMENDATION, KNOWN_FLAGS, flag_is_on
from app.schemas.hospital import FeatureFlagsResponse, FeatureFlagState

if TYPE_CHECKING:
    from app.database.unit_of_work import UnitOfWork
    from app.models.hospital import Hospital
    from app.repositories.hospital_repository import HospitalRepository
    from app.schemas.hospital import UpdateHospitalSettingsRequest

__all__ = ["HospitalService"]


class HospitalService:
    """Read and update the hospital a user belongs to.

    :param hospitals: Repository bound to the request session.
    :param uow: Transaction coordinator — settings writes commit atomically
        with their audit entry.
    :param audit: Sink every mutation is recorded against.
    :param ai_configured: Whether this server is configured to serve AI
        features. Decides, with the hospital's flag, what the capability read
        reports.
    """

    def __init__(
        self,
        hospitals: HospitalRepository,
        uow: UnitOfWork,
        audit: AuditSink,
        *,
        ai_configured: bool = False,
    ) -> None:
        self._hospitals = hospitals
        self._uow = uow
        self._audit = audit
        self._ai_configured = ai_configured

    async def get_current(self, hospital_id: uuid.UUID) -> Hospital:
        """Return the caller's hospital.

        :param hospital_id: Tenant to load.
        :returns: The active hospital row.
        :raises NotFoundError: If the hospital does not exist or is inactive.
        """
        hospital = await self._hospitals.get_active_by_id(hospital_id)
        if hospital is None:
            msg = "Hospital not found."
            raise NotFoundError(msg)
        return hospital

    async def get_feature_flags(self, hospital_id: uuid.UUID) -> FeatureFlagsResponse:
        """Report which gated features the caller's hospital can use right now.

        Returns only the well-known flags, one boolean each. A feature is
        available when the hospital's stored flag is exactly ``True`` and the
        server can serve it; the two reasons for "not available" are not told
        apart. Reads one row and calls no provider.

        :param hospital_id: Tenant to report on.
        :returns: The availability of each known flag.
        :raises NotFoundError: If the hospital does not exist or is inactive.
        """
        hospital = await self.get_current(hospital_id)
        # Flags that need something from the server as well as the hospital.
        server_ready = {AI_SLOT_RECOMMENDATION: self._ai_configured}
        return FeatureFlagsResponse(
            flags={
                key: FeatureFlagState(
                    available=flag_is_on(hospital.settings, key) and server_ready.get(key, True)
                )
                for key in KNOWN_FLAGS
            }
        )

    async def update_current(
        self,
        hospital_id: uuid.UUID,
        payload: UpdateHospitalSettingsRequest,
        *,
        actor_id: uuid.UUID,
    ) -> Hospital:
        """Apply an editable-fields update to the caller's hospital.

        Only fields present in the payload change; ``slug``, ``timezone``,
        ``currency`` and ``tax_id`` cannot reach here at all — the request
        schema forbids unknown keys (§9). The diff is captured before the
        write so the audit entry carries before/after values.

        :param hospital_id: Tenant to update.
        :param payload: Validated editable fields.
        :param actor_id: User performing the update.
        :returns: The updated hospital.
        """
        hospital = await self.get_current(hospital_id)

        updates = payload.model_dump(exclude_unset=True)
        if "settings" in updates:
            # Merge, never replace. ``settings`` is shared: the appointment
            # module keeps its no-show grace period and feature flags there,
            # billing its tax rate. A PATCH that sent one key used to wipe
            # every other one. A key sent as null is removed.
            merged = dict(hospital.settings or {})
            for key, value in updates["settings"].items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            updates["settings"] = merged

        changes: dict[str, dict[str, object]] = {}
        for field, new_value in updates.items():
            old_value = getattr(hospital, field)
            if old_value != new_value:
                changes[field] = {"before": old_value, "after": new_value}

        if not changes:
            return hospital

        hospital = await self._hospitals.update(hospital, **updates)

        await self._audit.record(
            AuditEvent(
                action="settings.hospital_updated",
                hospital_id=hospital.id,
                target_type="hospital",
                target_id=hospital.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._uow.commit()
        return hospital
