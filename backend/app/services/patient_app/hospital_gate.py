"""Which hospitals the Patient App may be used with.

A hospital is open to patients only when it is active **and** has the Patient
App switched on (``feature.patient_app.enabled``). Every other state — no such
hospital, an inactive one, the flag off or holding an unexpected value — is
one answer, ``None``, and callers turn it into the same "not found" a patient
gets for a record that does not match. Nothing here says which it was.

The directory of open hospitals is answered here too, so that there is one
rule and not two. A page of the directory has to be filtered by the database
— it is counted and paged there, under the patterns below — and every
hospital the database returns is then held to the same check as a hospital
named by its reference. A hospital is never listed, counted, or reflected in
the city options unless naming it would have found it.
"""

from __future__ import annotations

import re
import uuid
from typing import TYPE_CHECKING, Final

from app.core.feature_flags import PATIENT_APP_ENABLED, flag_is_on

if TYPE_CHECKING:
    from app.repositories.hospital_repository import (
        HospitalDirectoryEntry,
        HospitalRepository,
        HospitalSummary,
    )

__all__ = ["PatientHospitalGate"]

#: A hospital id exactly as ``str(uuid)`` writes it. Other spellings a UUID
#: parser would accept are not: one hospital has one reference.
_UUID_TEXT: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
#: A hospital slug: the code a hospital gives its patients.
_SLUG: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,99}$")


class PatientHospitalGate:
    """Resolves a hospital for a patient request, or refuses to.

    :param hospitals: Hospital data access.
    """

    def __init__(self, hospitals: HospitalRepository) -> None:
        self._hospitals = hospitals

    async def resolve(self, hospital_ref: str) -> HospitalSummary | None:
        """Find the enabled hospital a patient named by its id or its code.

        :param hospital_ref: The hospital UUID, or its slug.
        :returns: The hospital, or ``None`` if it cannot be used.
        """
        reference = hospital_ref.strip().lower()
        if _UUID_TEXT.fullmatch(reference):
            hospital = await self._hospitals.get_active_summary(id=uuid.UUID(reference))
        elif _SLUG.fullmatch(reference):
            hospital = await self._hospitals.get_active_summary(slug=reference)
        else:
            return None
        return self._if_enabled(hospital)

    async def get_enabled(self, hospital_id: uuid.UUID) -> HospitalSummary | None:
        """Find an enabled hospital by an id the server already holds.

        :param hospital_id: The hospital UUID.
        :returns: The hospital, or ``None`` if it cannot be used.
        """
        return self._if_enabled(await self._hospitals.get_active_summary(id=hospital_id))

    # ── Directory ────────────────────────────────────────────────────────────

    async def describe(self, hospital_ref: str) -> HospitalDirectoryEntry | None:
        """Find the enabled hospital a patient named, as the directory describes it.

        :param hospital_ref: The hospital UUID, or its slug.
        :returns: The directory entry, or ``None`` if the hospital cannot be used.
        """
        hospital = await self.resolve(hospital_ref)
        if hospital is None:
            return None
        return self._if_listed(
            await self._hospitals.get_directory_entry(hospital.id, PATIENT_APP_ENABLED)
        )

    async def list_open(
        self, *, search: str | None, city: str | None, skip: int, limit: int
    ) -> tuple[list[HospitalDirectoryEntry], int]:
        """One page of the hospitals that are open to patients.

        :param search: Text the hospital's name must contain, or ``None``.
        :param city: The city the hospital must be in, or ``None``.
        :param skip: Number of hospitals to skip.
        :param limit: Maximum hospitals to return.
        :returns: The page, in name order, and how many hospitals match in all.
        """
        entries, total = await self._hospitals.list_directory(
            PATIENT_APP_ENABLED,
            search=search,
            city=city,
            slug_like=_SLUG.pattern,
            slug_unlike=_UUID_TEXT.pattern,
            skip=skip,
            limit=limit,
        )
        # The database applied the same rule, so this drops nothing and the
        # total stands. It is kept so that a listed hospital is always one
        # this class has itself checked.
        return [entry for entry in entries if self._if_listed(entry) is not None], total

    async def open_cities(self, *, max_length: int, limit: int) -> list[str]:
        """The cities that have a hospital open to patients, in alphabetical order.

        Names only, gathered by the database under the filter that pages the
        directory: there is no hospital here to check a second time.

        :param max_length: Longer city names are left out.
        :param limit: Maximum number of cities to return.
        """
        return await self._hospitals.list_directory_cities(
            PATIENT_APP_ENABLED,
            max_length=max_length,
            limit=limit,
            slug_like=_SLUG.pattern,
            slug_unlike=_UUID_TEXT.pattern,
        )

    @staticmethod
    def public_ref(hospital: HospitalSummary) -> str | None:
        """The reference a patient can name a hospital by, if it has one.

        :param hospital: A hospital this gate has already found open.
        :returns: Its code — or ``None`` when the stored code is not one
            :meth:`resolve` could be given, so that no client is handed a
            reference that leads nowhere.
        """
        if _UUID_TEXT.fullmatch(hospital.slug) or not _SLUG.fullmatch(hospital.slug):
            return None
        return hospital.slug

    @staticmethod
    def _if_enabled[HospitalT: HospitalSummary](hospital: HospitalT | None) -> HospitalT | None:
        """Keep a hospital only if it has the Patient App switched on."""
        if hospital is None or not flag_is_on(hospital.settings, PATIENT_APP_ENABLED):
            return None
        return hospital

    @classmethod
    def _if_listed(cls, entry: HospitalDirectoryEntry | None) -> HospitalDirectoryEntry | None:
        """Keep a directory entry only if :meth:`resolve` would find it by its code.

        The directory hands out the code as the hospital's reference, so a
        hospital whose stored code is not one a reference can spell is left
        out rather than listed with a reference that leads nowhere.
        """
        entry = cls._if_enabled(entry)
        if entry is None or cls.public_ref(entry) is None:
            return None
        return entry
