"""Generic base repository providing common CRUD operations.

All business-module repositories inherit from :class:`BaseRepository`.
It provides type-safe, async CRUD with soft-delete and pagination support.

Repositories NEVER contain business logic. They only handle data access.

**Tenancy.** For a tenant-scoped model (one with a ``hospital_id`` column),
every primitive here that reads or changes rows by id or by list takes a
``scope``: the trusted hospital id, or an explicit
:func:`~app.core.tenancy.cross_tenant` marker. Calling one with no scope raises
:class:`~app.core.tenancy.TenantScopeRequiredError` — a missing tenant is never
turned into an unrestricted query. See :mod:`app.core.tenancy` for the contract
and for the second, ambient layer that confines the session itself.
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy import Select, UnaryExpression, func, select, update

from app.core.tenancy import (
    TENANT_COLUMN,
    CrossTenant,
    CrossTenantAccessError,
    TenantScopeRequiredError,
    is_tenant_scoped,
    tenant_column_nullable,
)
from app.database.base_class import Base
from app.models.base import SoftDeleteMixin

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.sql import ColumnElement


class BaseRepository[ModelT: Base]:
    """Generic repository providing common database operations.

    :param model_class: The SQLAlchemy model class this repository manages.
    :param session: An async SQLAlchemy session.
    """

    def __init__(self, model_class: type[ModelT], session: AsyncSession) -> None:
        self._model = model_class
        self._session = session
        self._tenant_scoped = is_tenant_scoped(model_class)

    # ── Tenancy ───────────────────────────────────────────────────────────────

    def _confine_to_tenant(
        self,
        stmt: Select[tuple[ModelT]],
        scope: uuid.UUID | CrossTenant | None,
        operation: str,
    ) -> Select[tuple[ModelT]]:
        """Confine ``stmt`` to one hospital, or fail if no tenant was given.

        :param stmt: The statement to confine.
        :param scope: The trusted hospital id, or a
            :func:`~app.core.tenancy.cross_tenant` marker for a deliberate
            cross-hospital lookup.
        :param operation: The calling primitive, for the error message.
        :returns: ``stmt`` filtered by ``hospital_id`` for a tenant-scoped
            model; unchanged for a model with no tenant column or for an
            explicit cross-tenant lookup.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and
            ``scope`` is ``None``.
        """
        if not self._tenant_scoped:
            return stmt
        if scope is None:
            msg = (
                f"{type(self).__name__}.{operation}() on tenant-scoped model "
                f"{self._model.__name__} needs a hospital id or an explicit cross_tenant(...)."
            )
            raise TenantScopeRequiredError(msg)
        if isinstance(scope, CrossTenant):
            return stmt
        return stmt.where(getattr(self._model, TENANT_COLUMN) == scope)

    # ── Query Building ────────────────────────────────────────────────────────

    def _soft_delete_supported(self) -> bool:
        """Check if the model class supports soft delete."""
        return issubclass(self._model, SoftDeleteMixin)

    def _apply_soft_delete_filter(self, stmt: Select[tuple[ModelT]]) -> Select[tuple[ModelT]]:
        """Append ``WHERE deleted_at IS NULL`` if the model supports soft delete."""
        if self._soft_delete_supported():
            return stmt.where(self._model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    def _apply_pagination(
        self,
        stmt: Select[tuple[ModelT]],
        skip: int = 0,
        limit: int = 100,
    ) -> Select[tuple[ModelT]]:
        """Apply offset/limit pagination."""
        return stmt.offset(skip).limit(limit)

    def _apply_ordering(
        self,
        stmt: Select[tuple[ModelT]],
        *order_by: UnaryExpression[Any],
    ) -> Select[tuple[ModelT]]:
        """Apply column ordering."""
        if order_by:
            return stmt.order_by(*order_by)
        return stmt.order_by(self._model.created_at.desc())  # type: ignore[attr-defined]

    # ── Base Query ────────────────────────────────────────────────────────────

    def _query(self) -> Select[tuple[ModelT]]:
        """Return a base ``SELECT *`` statement with soft-delete filtering."""
        return self._apply_soft_delete_filter(select(self._model))

    # ── CRUD Operations ───────────────────────────────────────────────────────

    async def create(self, **kwargs: Any) -> ModelT:
        """Create a new record and flush to the database.

        :param kwargs: Column values for the new record.
        :returns: The created ORM instance.
        :raises TenantScopeRequiredError: If the model is tenant-scoped, its
            ``hospital_id`` is ``NOT NULL``, and none was supplied.
        """
        if (
            self._tenant_scoped
            and not tenant_column_nullable(self._model)
            and kwargs.get(TENANT_COLUMN) is None
        ):
            msg = f"Cannot create a {self._model.__name__} without a {TENANT_COLUMN}."
            raise TenantScopeRequiredError(msg)
        instance = self._model(**kwargs)
        self._session.add(instance)
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def get_by_id(
        self, id: uuid.UUID, scope: uuid.UUID | CrossTenant | None = None
    ) -> ModelT | None:
        """Retrieve a record by its UUID primary key, within one hospital.

        :param id: The UUID of the record.
        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required for a tenant-scoped model.
        :returns: The ORM instance, or ``None`` if not found, soft-deleted, or
            owned by another hospital.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and no
            scope was given.
        """
        stmt = self._confine_to_tenant(
            self._query().where(self._model.id == id),  # type: ignore[attr-defined]
            scope,
            "get_by_id",
        )
        result = await self._session.execute(stmt)
        return result.unique().scalar_one_or_none()

    async def get_by_ids(
        self, ids: list[uuid.UUID], scope: uuid.UUID | CrossTenant | None = None
    ) -> list[ModelT]:
        """Retrieve multiple records by their UUID primary keys, within one hospital.

        :param ids: List of UUIDs.
        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required for a tenant-scoped model.
        :returns: List of found ORM instances (excludes soft-deleted and other
            hospitals' rows).
        :raises TenantScopeRequiredError: If the model is tenant-scoped and no
            scope was given.
        """
        stmt = self._confine_to_tenant(
            self._query().where(self._model.id.in_(ids)),  # type: ignore[attr-defined]
            scope,
            "get_by_ids",
        )
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def list(
        self,
        *,
        scope: uuid.UUID | CrossTenant | None = None,
        skip: int = 0,
        limit: int = 100,
        order_by: ColumnElement[Any] | None = None,
    ) -> list[ModelT]:
        """List records with pagination, within one hospital.

        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required for a tenant-scoped model.
        :param skip: Number of records to skip (offset).
        :param limit: Maximum number of records to return.
        :param order_by: Optional column expression for ordering.
        :returns: List of ORM instances.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and no
            scope was given.
        """
        stmt = self._confine_to_tenant(self._query(), scope, "list")
        if order_by is not None:
            stmt = stmt.order_by(order_by)
        stmt = self._apply_pagination(stmt, skip=skip, limit=limit)
        result = await self._session.execute(stmt)
        return list(result.unique().scalars().all())

    async def update(self, instance: ModelT, **kwargs: Any) -> ModelT:
        """Update a record with the provided field values.

        The instance is modified in-place and flushed to the database.

        The instance was already loaded through a tenant-scoped read, so it
        carries its own hospital. That hospital cannot be changed here.

        :param instance: The ORM instance to update (must be attached to session).
        :param kwargs: Field names and their new values.
        :returns: The updated ORM instance.
        :raises CrossTenantAccessError: If ``kwargs`` would move the row to
            another hospital.
        """
        self._refuse_tenant_change(instance, kwargs)
        for field, value in kwargs.items():
            if hasattr(instance, field):
                setattr(instance, field, value)
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def update_by_pk(
        self,
        id: uuid.UUID,
        scope: uuid.UUID | CrossTenant | None = None,
        **kwargs: Any,
    ) -> ModelT | None:
        """Update a record identified by its primary key, within one hospital.

        :param id: The UUID of the record to update.
        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required for a tenant-scoped model.
        :param kwargs: Field names and their new values.
        :returns: The updated ORM instance, or ``None`` if no row of that
            hospital has this id.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and no
            scope was given.
        :raises CrossTenantAccessError: If ``kwargs`` names the tenant column.
        """
        if self._tenant_scoped and TENANT_COLUMN in kwargs:
            msg = f"{self._model.__name__}.{TENANT_COLUMN} cannot be changed."
            raise CrossTenantAccessError(msg)
        stmt = update(self._model).where(self._model.id == id)  # type: ignore[attr-defined]
        if self._tenant_scoped:
            if scope is None:
                msg = (
                    f"{type(self).__name__}.update_by_pk() on tenant-scoped model "
                    f"{self._model.__name__} needs a hospital id or an explicit cross_tenant(...)."
                )
                raise TenantScopeRequiredError(msg)
            if not isinstance(scope, CrossTenant):
                stmt = stmt.where(getattr(self._model, TENANT_COLUMN) == scope)
        stmt = stmt.values(**kwargs).returning(self._model)
        result = await self._session.execute(stmt)
        await self._session.flush()
        row = result.unique().scalar_one_or_none()
        if row is not None:
            await self._session.refresh(row)
        return row

    async def soft_delete(self, instance: ModelT) -> ModelT:
        """Soft-delete a record (sets ``deleted_at``).

        :param instance: The ORM instance to soft-delete.
        :returns: The soft-deleted ORM instance.
        :raises TypeError: If the model does not support soft delete.
        """
        if not self._soft_delete_supported():
            msg = f"{self._model.__name__} does not support soft delete."
            raise TypeError(msg)
        instance.deleted_at = func.now()  # type: ignore[attr-defined]
        await self._session.flush()
        await self._session.refresh(instance)
        return instance

    async def hard_delete(self, instance: ModelT) -> None:
        """Permanently delete a record from the database.

        Use sparingly. Most business tables should use :meth:`soft_delete`.

        :param instance: The ORM instance to permanently delete.
        """
        await self._session.delete(instance)
        await self._session.flush()

    async def count(
        self,
        stmt: Select[tuple[ModelT]] | None = None,
        *,
        scope: uuid.UUID | CrossTenant | None = None,
    ) -> int:
        """Count records matching an optional filter.

        :param stmt: Optional select statement, already confined to a hospital
            by the repository method that built it. Defaults to the base
            query, which for a tenant-scoped model needs ``scope``.
        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required when ``stmt`` is omitted on a tenant-scoped model.
        :returns: The record count.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and
            neither a statement nor a scope was given.
        """
        if stmt is None or scope is not None:
            stmt = self._confine_to_tenant(
                stmt if stmt is not None else select(self._model), scope, "count"
            )
        query = self._apply_soft_delete_filter(stmt)
        count_stmt = select(func.count()).select_from(query.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one()

    async def exists(self, id: uuid.UUID, scope: uuid.UUID | CrossTenant | None = None) -> bool:
        """Check if a record with the given ID exists in one hospital.

        :param id: The UUID to check.
        :param scope: The trusted hospital id, or a ``cross_tenant(...)``
            marker. Required for a tenant-scoped model.
        :returns: ``True`` if the record exists, is not soft-deleted, and
            belongs to that hospital.
        :raises TenantScopeRequiredError: If the model is tenant-scoped and no
            scope was given.
        """
        stmt = self._confine_to_tenant(
            self._query().where(self._model.id == id),  # type: ignore[attr-defined]
            scope,
            "exists",
        )
        count_stmt = select(func.count()).select_from(stmt.subquery())
        result = await self._session.execute(count_stmt)
        return result.scalar_one() > 0

    def _refuse_tenant_change(self, instance: ModelT, values: dict[str, Any]) -> None:
        """Refuse a field update that would move a row to another hospital."""
        if not self._tenant_scoped or TENANT_COLUMN not in values:
            return
        if values[TENANT_COLUMN] != getattr(instance, TENANT_COLUMN):
            msg = f"{self._model.__name__}.{TENANT_COLUMN} cannot be changed on an existing row."
            raise CrossTenantAccessError(msg)
