"""Repository for the :class:`Hospital` model.

Hospitals are the multi-tenant root. They are deactivated via
:attr:`~Hospital.is_active` rather than soft-deleted.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import case, func, select

from app.models.hospital import Hospital
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy import Row
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import InstrumentedAttribute
    from sqlalchemy.sql import ColumnElement

#: Every character ``str.strip()`` removes. The city in a stored address is
#: trimmed with exactly this set, so "trimmed" means one thing in a query and
#: in Python.
WHITESPACE: Final = "".join(char for char in map(chr, range(0x3001)) if char.isspace())


@dataclass(frozen=True, slots=True)
class HospitalSummary:
    """What identifies a hospital, without its users, roles or contact details.

    :param id: The hospital UUID.
    :param name: Display name.
    :param slug: Unique URL-friendly identifier.
    :param settings: The hospital's settings object (feature flags live here).
    """

    id: uuid.UUID
    name: str
    slug: str
    settings: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HospitalDirectoryEntry(HospitalSummary):
    """A hospital as a directory may describe it: what identifies it, and where it is.

    Still columns, not the entity — no users and no roles — and none of the
    columns a directory never shows (tax id, e-mail, currency, audit columns).

    :param address: The stored address object, as stored.
    :param phone: Primary contact phone, if any.
    :param logo_url: The stored logo URL, if any. Not checked here.
    :param timezone: IANA timezone.
    """

    address: Any
    phone: str | None
    logo_url: str | None
    timezone: str


def _settings_object(stored: Any) -> dict[str, Any]:
    """A copy of a stored settings value — empty if it is anything but an object.

    The column holds an object, but nothing in the database says so. A row
    holding a list or a string has no flag switched on; it is not an error.
    """
    return dict(stored) if isinstance(stored, dict) else {}


class HospitalRepository(BaseRepository[Hospital]):
    """Repository for hospital CRUD operations.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Hospital, session)

    async def create(  # type: ignore[override]
        self,
        name: str,
        slug: str,
        address: dict[str, Any],
        **kwargs: object,
    ) -> Hospital:
        """Create a new hospital.

        :param name: Full hospital name.
        :param slug: URL-friendly unique identifier.
        :param address: Structured address object.
        :param kwargs: Additional optional fields (phone, email, tax_id, etc.).
        :returns: The created hospital instance.
        """
        return await super().create(
            name=name,
            slug=slug,
            address=address,
            **kwargs,
        )

    async def get_by_slug(self, slug: str) -> Hospital | None:
        """Retrieve a hospital by its slug.

        :param slug: The unique URL-friendly identifier.
        :returns: The hospital instance, or ``None``.
        """
        stmt = select(Hospital).where(Hospital.slug == slug)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_active_by_id(self, id: uuid.UUID) -> Hospital | None:
        """Retrieve an active hospital by ID.

        :param id: The hospital UUID.
        :returns: The hospital instance if active, or ``None``.
        """
        stmt = select(Hospital).where(Hospital.id == id, Hospital.is_active.is_(True))
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_active_summary(
        self, *, id: uuid.UUID | None = None, slug: str | None = None
    ) -> HospitalSummary | None:
        """Read the few columns that identify one active hospital.

        Selects columns, not the entity: loading a :class:`Hospital` also
        loads every user and role it has (``lazy="selectin"``), which a caller
        that only needs the name and the feature flags must not pay for — and
        which the Patient App must not pull into a patient's request at all.

        :param id: The hospital UUID. Exactly one of ``id`` and ``slug`` is given.
        :param slug: The hospital's unique slug.
        :returns: The summary, or ``None`` if there is no such active hospital.
        :raises ValueError: If neither or both of ``id`` and ``slug`` are given.
        """
        if (id is None) == (slug is None):
            msg = "get_active_summary() needs exactly one of id and slug."
            raise ValueError(msg)
        stmt = select(Hospital.id, Hospital.name, Hospital.slug, Hospital.settings).where(
            Hospital.is_active.is_(True)
        )
        stmt = (
            stmt.where(Hospital.id == id) if id is not None else stmt.where(Hospital.slug == slug)
        )
        row = (await self._session.execute(stmt)).one_or_none()
        if row is None:
            return None
        return HospitalSummary(
            id=row.id, name=row.name, slug=row.slug, settings=_settings_object(row.settings)
        )

    # ── Directory ────────────────────────────────────────────────────────────
    # Active hospitals that have one feature flag switched on, as columns. The
    # flag is the caller's: this repository does not know which features exist.

    async def list_directory(
        self,
        flag: str,
        *,
        search: str | None = None,
        city: str | None = None,
        slug_like: str | None = None,
        slug_unlike: str | None = None,
        skip: int = 0,
        limit: int = 20,
    ) -> tuple[list[HospitalDirectoryEntry], int]:
        """One page of the active hospitals that have a flag switched on.

        Ordered by name without regard to case, then by slug — which is
        unique, so the order is total and a page never repeats or skips a row.

        :param flag: The feature flag a hospital must have switched on.
        :param search: Text the name must contain, case-insensitively. Matched
            literally: ``%``, ``_`` and ``\\`` are not wildcards.
        :param city: The city the address must name, case-insensitively and in
            full.
        :param slug_like: A regular expression the slug must match, or ``None``.
        :param slug_unlike: A regular expression the slug must not match, or
            ``None``.
        :param skip: Number of rows to skip.
        :param limit: Maximum rows to return.
        :returns: The page, and how many hospitals match in all.
        """
        filters = [*self._active_with_flag(flag), *self._slug_rule(slug_like, slug_unlike)]
        if search:
            # ``escape`` is set so a term containing % or _ is matched
            # literally rather than acting as a wildcard.
            literal = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append(Hospital.name.ilike(f"%{literal}%", escape="\\"))
        if city:
            filters.append(func.lower(self._city()) == func.lower(city))

        total = await self._session.execute(
            select(func.count()).select_from(Hospital).where(*filters)
        )
        rows = await self._session.execute(
            select(*self._directory_columns())
            .where(*filters)
            .order_by(func.lower(Hospital.name), Hospital.slug)
            .offset(skip)
            .limit(limit)
        )
        return [self._directory_entry(row) for row in rows], total.scalar_one()

    async def get_directory_entry(self, id: uuid.UUID, flag: str) -> HospitalDirectoryEntry | None:
        """Read one active hospital that has a flag switched on.

        :param id: The hospital UUID.
        :param flag: The feature flag the hospital must have switched on.
        :returns: The entry, or ``None`` if there is no such hospital.
        """
        row = (
            await self._session.execute(
                select(*self._directory_columns()).where(
                    Hospital.id == id, *self._active_with_flag(flag)
                )
            )
        ).one_or_none()
        return None if row is None else self._directory_entry(row)

    async def list_directory_cities(
        self,
        flag: str,
        *,
        max_length: int,
        limit: int,
        slug_like: str | None = None,
        slug_unlike: str | None = None,
    ) -> list[str]:
        """The cities of the active hospitals that have a flag switched on.

        Each city once, whatever its capitalisation, in alphabetical order.

        :param flag: The feature flag a hospital must have switched on.
        :param max_length: Longer values are left out.
        :param limit: Maximum number of cities to return.
        :param slug_like: A regular expression the slug must match, or ``None``.
        :param slug_unlike: A regular expression the slug must not match, or
            ``None``.
        :returns: Trimmed, non-empty city names.
        """
        directory = (
            select(self._city().label("city"))
            .where(*self._active_with_flag(flag), *self._slug_rule(slug_like, slug_unlike))
            .subquery()
        )
        folded = func.lower(directory.c.city)
        result = await self._session.execute(
            select(func.min(directory.c.city))
            .where(directory.c.city != "", func.char_length(directory.c.city) <= max_length)
            .group_by(folded)
            .order_by(folded)
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    def _active_with_flag(flag: str) -> tuple[ColumnElement[bool], ...]:
        """The hospital is active and the flag's stored value is exactly ``true``.

        The query form of :func:`app.core.feature_flags.flag_is_on`. JSONB
        containment compares types as well as values, so a flag stored as the
        string ``"true"`` or the number ``1`` is not on.
        """
        return (Hospital.is_active.is_(True), Hospital.settings.contains({flag: True}))

    @staticmethod
    def _slug_rule(like: str | None, unlike: str | None) -> tuple[ColumnElement[bool], ...]:
        """Keep the hospitals whose slug matches one pattern and not the other.

        The patterns are the caller's, as the flag is. They are matched by the
        database so that a page, its total and the cities are all counted over
        the same hospitals; PostgreSQL reads a bracket range such as ``[a-z]``
        by code point, as Python does.
        """
        rule: list[ColumnElement[bool]] = []
        if like is not None:
            rule.append(Hospital.slug.regexp_match(like))
        if unlike is not None:
            rule.append(~Hospital.slug.regexp_match(unlike))
        return tuple(rule)

    @staticmethod
    def _city() -> ColumnElement[Any]:
        """The trimmed city of the stored address, or ``NULL`` if it is not text."""
        return case(
            (
                func.jsonb_typeof(func.jsonb_extract_path(Hospital.address, "city")) == "string",
                func.btrim(func.jsonb_extract_path_text(Hospital.address, "city"), WHITESPACE),
            ),
            else_=None,
        )

    @staticmethod
    def _directory_columns() -> tuple[InstrumentedAttribute[Any], ...]:
        """The columns a :class:`HospitalDirectoryEntry` is built from, and no others."""
        return (
            Hospital.id,
            Hospital.name,
            Hospital.slug,
            Hospital.settings,
            Hospital.address,
            Hospital.phone,
            Hospital.logo_url,
            Hospital.timezone,
        )

    @staticmethod
    def _directory_entry(row: Row[Any]) -> HospitalDirectoryEntry:
        """Build an entry from a row of :meth:`_directory_columns`."""
        return HospitalDirectoryEntry(
            id=row.id,
            name=row.name,
            slug=row.slug,
            settings=_settings_object(row.settings),
            address=row.address,
            phone=row.phone,
            logo_url=row.logo_url,
            timezone=row.timezone,
        )

    async def deactivate(self, hospital: Hospital) -> Hospital:
        """Deactivate a hospital by setting ``is_active = False``.

        :param hospital: The hospital instance to deactivate.
        :returns: The updated hospital instance.
        """
        return await self.update(hospital, is_active=False)

    async def list_active(self, skip: int = 0, limit: int = 100) -> list[Hospital]:
        """List only active hospitals.

        :param skip: Number of records to skip.
        :param limit: Maximum records to return.
        :returns: List of active hospital instances.
        """
        stmt = select(Hospital).where(Hospital.is_active.is_(True))
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_active(self) -> int:
        """Count active hospitals."""
        stmt = select(Hospital).where(Hospital.is_active.is_(True))
        return await self.count(stmt)
