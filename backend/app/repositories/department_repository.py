"""Repository for the :class:`~app.models.department.Department` model.

Data access only — no business rules, no HTTP exceptions, ORM models out
(``docs/03-ARCHITECTURE.md`` §4.4). Every method takes ``hospital_id`` and
filters on it: multi-tenant isolation is enforced on the way into the database,
never assumed from the caller (CLAUDE.md rule 5).

.. note::

   Scoping is passed explicitly, as an argument, on every method here. That is
   one of two layers: ``app/core/tenancy.py`` also confines the session itself
   to the authenticated principal's hospital, so a query that passed the wrong
   ``hospital_id`` — or forgot the filter — still cannot reach another
   hospital's rows.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, func, or_, select

from app.models.department import Department
from app.repositories.base import BaseRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


class DepartmentRepository(BaseRepository[Department]):
    """Repository for department persistence.

    :param session: An active async SQLAlchemy session.
    """

    def __init__(self, session: AsyncSession) -> None:
        super().__init__(Department, session)

    # ── Query building ────────────────────────────────────────────────────────

    def _scoped(
        self, hospital_id: uuid.UUID, *, include_deleted: bool = False
    ) -> Select[tuple[Department]]:
        """Return a base SELECT filtered to one hospital.

        :param hospital_id: The tenant to scope to.
        :param include_deleted: When ``True``, soft-deleted rows are included.
            Needed by the duplicate checks and the reactivation path.
        :returns: A statement filtered by ``hospital_id`` and, by default,
            ``deleted_at IS NULL``.
        """
        stmt = select(Department) if include_deleted else self._query()
        return stmt.where(Department.hospital_id == hospital_id)

    def _apply_search_filters(
        self,
        stmt: Select[tuple[Department]],
        *,
        term: str | None = None,
    ) -> Select[tuple[Department]]:
        """Apply the search predicates shared by list, search, and count.

        ``term`` is a case-insensitive **prefix** match on name and an exact
        match on code. Prefix rather than substring so the ``lower(name)``
        index from migration 0005 is usable; exact on code because a partial
        code is not a meaningful query.

        :param stmt: The statement to extend.
        :param term: Free-text search term.
        :returns: The statement with predicates applied.
        """
        if term:
            needle = term.lower()
            # ``escape`` is set so a term containing % or _ is matched
            # literally rather than acting as a wildcard.
            prefix = needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            stmt = stmt.where(
                or_(
                    func.lower(Department.name).like(prefix, escape="\\"),
                    Department.code == term.upper(),
                )
            )
        return stmt

    @staticmethod
    def _ordered(stmt: Select[tuple[Department]]) -> Select[tuple[Department]]:
        """Order by name, with ``id`` as a tiebreaker.

        The ``id`` tiebreak makes pagination stable: without it two departments
        with identical names could swap places between page requests, showing a
        record twice or skipping it entirely.
        """
        return stmt.order_by(Department.name.asc(), Department.id.asc())

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_department(
        self,
        *,
        hospital_id: uuid.UUID,
        code: str,
        name: str,
        created_by: uuid.UUID | None = None,
        **optional_fields: Any,
    ) -> Department:
        """Insert a new department.

        Does not commit — the service owns the transaction
        (``docs/03-ARCHITECTURE.md`` §9).

        :param hospital_id: Owning tenant.
        :param code: Short department code, already uppercased.
        :param name: Department name.
        :param created_by: UUID of the acting user.
        :param optional_fields: Any remaining department columns.
        :returns: The persisted department.
        """
        return await super().create(
            hospital_id=hospital_id,
            code=code,
            name=name,
            created_by=created_by,
            **optional_fields,
        )

    async def update_department(
        self,
        department: Department,
        *,
        updated_by: uuid.UUID | None = None,
        **fields: Any,
    ) -> Department:
        """Apply field updates to an existing department.

        :param department: The attached ORM instance to modify.
        :param updated_by: UUID of the acting user.
        :param fields: Column names and their new values.
        :returns: The updated department.
        """
        return await self.update(department, updated_by=updated_by, **fields)

    async def delete_department(
        self,
        department: Department,
        *,
        deleted_by: uuid.UUID | None = None,
    ) -> Department:
        """Soft-delete a department and record who did it.

        Departments are never hard-deleted (module spec §4, rule 12): doctors
        and appointments keep referencing them, and a hard delete would orphan
        that history.

        :param department: The department to deactivate.
        :param deleted_by: UUID of the acting user.
        :returns: The soft-deleted department.
        """
        department.deleted_by = deleted_by
        return await self.soft_delete(department)

    async def restore_department(
        self,
        department: Department,
        *,
        updated_by: uuid.UUID | None = None,
    ) -> Department:
        """Clear the soft delete on a department, reactivating the record.

        :param department: The soft-deleted department to restore.
        :param updated_by: UUID of the acting user.
        :returns: The reactivated department.
        """
        return await self.update(
            department,
            deleted_at=None,
            deleted_by=None,
            updated_by=updated_by,
        )

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_department_by_id(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> Department | None:
        """Retrieve one department by UUID within a hospital.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department UUID.
        :param include_deleted: Include soft-deleted records (needed to
            reactivate one).
        :returns: The department, or ``None`` if absent or in another tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Department.id == department_id
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_department_by_code(
        self,
        hospital_id: uuid.UUID,
        code: str,
        *,
        include_deleted: bool = True,
    ) -> Department | None:
        """Retrieve one department by code within a hospital.

        Defaults to including soft-deleted rows: ``uq_departments_hospital_code``
        applies regardless of deletion state, so a duplicate check that ignored
        deactivated departments would report "available" for a code the unique
        index will reject.

        :param hospital_id: The tenant to scope to.
        :param code: The department code. Compared as given — the service
            uppercases before calling.
        :param include_deleted: Include soft-deleted records.
        :returns: The department, or ``None``.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Department.code == code
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_department_by_name(
        self,
        hospital_id: uuid.UUID,
        name: str,
        *,
        include_deleted: bool = True,
    ) -> Department | None:
        """Retrieve one department by name within a hospital, case-insensitively.

        Matches ``uq_departments_hospital_name_lower``, which is a functional
        index on ``lower(name)`` — so this comparison and the constraint agree
        on what counts as a duplicate.

        :param hospital_id: The tenant to scope to.
        :param name: The department name.
        :param include_deleted: Include soft-deleted records.
        :returns: The department, or ``None``.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            func.lower(Department.name) == name.lower()
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def list_departments(
        self,
        hospital_id: uuid.UUID,
        *,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Department]:
        """List departments in a hospital, ordered by name.

        :param hospital_id: The tenant to scope to.
        :param include_deleted: Include deactivated departments.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of departments.
        """
        stmt = self._ordered(self._scoped(hospital_id, include_deleted=include_deleted))
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def search_departments(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        include_deleted: bool = False,
        skip: int = 0,
        limit: int = 25,
    ) -> list[Department]:
        """Search departments within a hospital.

        See :meth:`_apply_search_filters` for the matching rules.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term (name prefix or exact code).
        :param include_deleted: Include deactivated departments.
        :param skip: Records to skip (offset).
        :param limit: Maximum records to return.
        :returns: A page of matching departments.
        """
        stmt = self._apply_search_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
        )
        stmt = self._apply_pagination(self._ordered(stmt), skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def count_departments(
        self,
        hospital_id: uuid.UUID,
        *,
        term: str | None = None,
        include_deleted: bool = False,
    ) -> int:
        """Count departments matching the same filters :meth:`search_departments` uses.

        Takes the identical filter arguments so the total in a paginated
        response can never disagree with the rows on the page.

        :param hospital_id: The tenant to scope to.
        :param term: Free-text term.
        :param include_deleted: Include deactivated departments.
        :returns: The number of matching departments.
        """
        stmt = self._apply_search_filters(
            self._scoped(hospital_id, include_deleted=include_deleted),
            term=term,
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def department_exists(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        *,
        include_deleted: bool = False,
    ) -> bool:
        """Check whether a department exists in a hospital.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department UUID.
        :param include_deleted: Count deactivated departments as existing.
        :returns: ``True`` if the department exists in this tenant.
        """
        stmt = self._scoped(hospital_id, include_deleted=include_deleted).where(
            Department.id == department_id
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one() > 0
