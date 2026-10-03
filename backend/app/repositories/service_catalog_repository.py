"""Repository for the billable services catalog.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it (CLAUDE.md rules 4 and 5).

Named ``service_catalog`` rather than ``service`` because "service" already
means the business-logic layer everywhere else in this codebase.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, select

from app.models.billing import Service
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession


class ServiceCatalogRepository(BaseRepository[Service]):
    """Persistence for a hospital's billable services.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Service, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[Service]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(Service.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[Service]],
        *,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Select[tuple[Service]]:
        """Apply the filters shared by list and count (module spec §9).

        ``term`` is a case-insensitive **prefix** match on name and an exact
        match on code — the same rule the department and doctor lists use.

        :param stmt: The statement to extend.
        :param term: Free-text search term.
        :param category: Exact category filter.
        :param is_active: Filter on the active flag. ``None`` returns both.
        :returns: The statement with predicates applied.
        """
        if term:
            # ``escape`` is set so a term containing % or _ is matched
            # literally rather than acting as a wildcard.
            prefix = (
                term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            )
            stmt = stmt.where(
                or_(
                    func.lower(Service.name).like(prefix, escape="\\"),
                    Service.code == term.upper(),
                )
            )
        if category is not None:
            stmt = stmt.where(Service.category == category)
        if is_active is not None:
            stmt = stmt.where(Service.is_active.is_(is_active))
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Service]]) -> Select[tuple[Service]]:
        """Order by name, with ``id`` as a stable tiebreaker for pagination."""
        return stmt.order_by(Service.name.asc(), Service.id.asc())

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_service(
        self,
        *,
        hospital_id: uuid.UUID,
        code: str,
        name: str,
        price: Decimal,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Service:
        """Insert a catalog service.

        Does not commit — the service layer owns the transaction.

        :param hospital_id: Owning tenant.
        :param code: Catalog code, already uppercased.
        :param name: Display name.
        :param price: Unit price.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns (category, taxable, is_active).
        :returns: The persisted service.
        """
        return await super().create(
            hospital_id=hospital_id,
            code=code,
            name=name,
            price=price,
            created_by=created_by,
            **optional_fields,
        )

    async def update_service(
        self, service: Service, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> Service:
        """Apply field updates to an existing catalog service.

        :param service: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated service.
        """
        return await self.update(service, updated_by=updated_by, **fields)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_service_by_id(
        self, hospital_id: uuid.UUID, service_id: uuid.UUID
    ) -> Service | None:
        """Retrieve one catalog service by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param service_id: The service UUID.
        :returns: The service, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(Service.id == service_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_services_by_ids(
        self, hospital_id: uuid.UUID, service_ids: Sequence[uuid.UUID]
    ) -> list[Service]:
        """Retrieve several catalog services in one query.

        Used when pricing an invoice, so a ten-line invoice costs one lookup
        rather than ten.

        :param hospital_id: The tenant to scope to.
        :param service_ids: The service UUIDs wanted.
        :returns: The services found. Ids from another tenant are simply absent.
        """
        if not service_ids:
            return []
        stmt = self._scoped(hospital_id).where(Service.id.in_(service_ids))
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def get_service_by_code(self, hospital_id: uuid.UUID, code: str) -> Service | None:
        """Retrieve a catalog service by its code within a hospital.

        :param hospital_id: The tenant to scope to.
        :param code: The catalog code, already uppercased.
        :returns: The service, or ``None``.
        """
        stmt = self._scoped(hospital_id).where(Service.code == code)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_services(
        self,
        hospital_id: uuid.UUID,
        *,
        skip: int = 0,
        limit: int = 25,
        **filters: Any,
    ) -> list[Service]:
        """List catalog services in a hospital, ordered by name.

        :param hospital_id: The tenant to scope to.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: A page of services.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_services(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count catalog services matching the filters :meth:`list_services` uses.

        :param hospital_id: The tenant to scope to.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: The number of matching services.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()
