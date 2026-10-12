"""Repository for the lab test catalog.

Data access only: no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it (CLAUDE.md rules 4 and 5).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, select

from app.models.lab import LabTest
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession


class LabTestRepository(BaseRepository[LabTest]):
    """Persistence for a hospital's orderable tests.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(LabTest, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(self, hospital_id: uuid.UUID) -> Select[tuple[LabTest]]:
        """Return a base SELECT filtered to one hospital."""
        return self._query().where(LabTest.hospital_id == hospital_id)

    @staticmethod
    def _apply_filters(
        stmt: Select[tuple[LabTest]],
        *,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Select[tuple[LabTest]]:
        """Apply the filters shared by list and count.

        ``term`` is a case-insensitive **prefix** match on name and an exact
        match on code — the same rule the services catalog uses.

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
                    func.lower(LabTest.name).like(prefix, escape="\\"),
                    LabTest.code == term.upper(),
                )
            )
        if category is not None:
            stmt = stmt.where(LabTest.category == category)
        if is_active is not None:
            stmt = stmt.where(LabTest.is_active.is_(is_active))
        return stmt

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_test(
        self,
        *,
        hospital_id: uuid.UUID,
        code: str,
        name: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> LabTest:
        """Insert a catalog test.

        Does not commit — the service layer owns the transaction.

        :param hospital_id: Owning tenant.
        :param code: Catalog code, already uppercased.
        :param name: Display name.
        :param created_by: UUID of the acting user.
        :param optional_fields: Remaining columns.
        :returns: The persisted test.
        """
        return await super().create(
            hospital_id=hospital_id, code=code, name=name, created_by=created_by, **optional_fields
        )

    async def update_test(
        self, test: LabTest, *, updated_by: uuid.UUID | None = None, **fields: Any
    ) -> LabTest:
        """Apply field updates to an existing catalog test.

        :param test: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated test.
        """
        return await self.update(test, updated_by=updated_by, **fields)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_test_by_id(self, hospital_id: uuid.UUID, test_id: uuid.UUID) -> LabTest | None:
        """Retrieve one catalog test by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param test_id: The test UUID.
        :returns: The test, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id).where(LabTest.id == test_id)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_tests_by_ids(
        self, hospital_id: uuid.UUID, test_ids: Sequence[uuid.UUID]
    ) -> list[LabTest]:
        """Retrieve several catalog tests in one query.

        :param hospital_id: The tenant to scope to.
        :param test_ids: The test UUIDs wanted.
        :returns: The tests found. Ids from another tenant are simply absent.
        """
        if not test_ids:
            return []
        stmt = self._scoped(hospital_id).where(LabTest.id.in_(test_ids))
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def get_test_by_code(self, hospital_id: uuid.UUID, code: str) -> LabTest | None:
        """Retrieve a catalog test by its code within a hospital.

        :param hospital_id: The tenant to scope to.
        :param code: The catalog code, already uppercased.
        :returns: The test, or ``None``.
        """
        stmt = self._scoped(hospital_id).where(LabTest.code == code)
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_tests(
        self, hospital_id: uuid.UUID, *, skip: int = 0, limit: int = 25, **filters: Any
    ) -> list[LabTest]:
        """List catalog tests in a hospital, ordered by name.

        :param hospital_id: The tenant to scope to.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: A page of tests.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        stmt = self._apply_pagination(
            stmt.order_by(LabTest.name.asc(), LabTest.id.asc()), skip=skip, limit=limit
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_tests(self, hospital_id: uuid.UUID, **filters: Any) -> int:
        """Count catalog tests matching the filters :meth:`list_tests` uses.

        :param hospital_id: The tenant to scope to.
        :param filters: Any of the predicates :meth:`_apply_filters` accepts.
        :returns: The number of matching tests.
        """
        stmt = self._apply_filters(self._scoped(hospital_id), **filters)
        result = await self._session.execute(select(func.count()).select_from(stmt.subquery()))
        return result.scalar_one()
