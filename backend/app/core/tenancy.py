"""Hospital tenancy — the one place that defines and enforces the tenant boundary.

**The contract.** A tenant-scoped operation must not be able to return or
mutate a row that belongs to another hospital. The hospital always comes from
a *trusted* source — the authenticated principal, or a system job that read it
off the row it is processing — and never from a value the client chose.

Two independent layers enforce it. Either one alone stops a cross-tenant
access; they fail in different ways, so a mistake in one is caught by the other.

**Layer 1 — explicit scope at the repository** (``app/repositories/base.py``).
Every base-repository primitive on a tenant-scoped model takes a *scope*: a
hospital id, or an explicit :class:`CrossTenant` marker built with
:func:`cross_tenant`. Passing nothing raises
:class:`TenantScopeRequiredError` — a missing tenant is an error, never an
unrestricted query.

**Layer 2 — ambient scope on the session** (this module). When a
:class:`TenantScope` is bound for the current task, every ORM statement that
touches a tenant-scoped entity gets ``hospital_id = <scope>`` added
automatically, and a flush that would write a row of another hospital is
refused. This does not depend on the repository author remembering a ``WHERE``
clause or on the caller passing the right id.

How a scope is bound, per kind of caller:

=========================  ====================================================
Caller                     Scope
=========================  ====================================================
Staff principal            ``TenantScope.hospital(user.hospital_id)``, bound by
                           ``get_current_user`` once the token and the user row
                           are verified.
Super Admin / platform     A principal with no hospital gets
                           ``TenantScope.system(...)``: no ambient filter, so
                           cross-hospital administration keeps working. It must
                           still pass an explicit :func:`cross_tenant` marker at
                           the repository.
Future patient principal   ``TenantScope.hospital(link.hospital_id)`` — the
                           hospital of the server-resolved record link
                           (``docs/modules/15-patient-app.md`` §5.7), never a
                           client-supplied id.
Background / scheduled job No user session. A job discovers work under
                           ``TenantScope.system(...)`` and then processes each
                           row under ``TenantScope.hospital(row.hospital_id)``,
                           so one unit of work cannot touch two hospitals.
Unauthenticated request    Nothing is bound. Login, token refresh and password
                           reset resolve an identity first; they reach tenant
                           data only through explicit :func:`cross_tenant`
                           lookups.
=========================  ====================================================

When nothing is bound, Layer 2 does nothing and Layer 1 is the control. That
is deliberate: tests, seeds and one-off scripts call services directly with an
explicit hospital id, and must not need a fake principal.

This module adds no permissions and no roles. It is orthogonal to RBAC: RBAC
decides *whether* a principal may perform an action, tenancy decides *which
hospital's rows* the action can ever see.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final, cast

from sqlalchemy import event, inspect, or_
from sqlalchemy.orm import Session, with_loader_criteria
from sqlalchemy.sql import visitors

if TYPE_CHECKING:
    import uuid
    from collections.abc import Iterator

    from sqlalchemy.orm import ORMExecuteState
    from sqlalchemy.sql import ColumnElement
    from sqlalchemy.sql.visitors import ExternallyTraversible

__all__ = [
    "TENANT_COLUMN",
    "CrossTenant",
    "CrossTenantAccessError",
    "TenantScope",
    "TenantScopeRequiredError",
    "bind_tenant_scope",
    "cross_tenant",
    "current_tenant_scope",
    "install_tenant_guards",
    "is_tenant_scoped",
    "scope_for_principal",
    "tenant_column_nullable",
    "tenant_or_platform",
    "tenant_scope",
]

#: The column that carries the tenant on every tenant-scoped table
#: (architectural rule 4).
TENANT_COLUMN: Final = "hospital_id"

#: The table whose primary key *is* the tenant. It has no ``hospital_id``; the
#: ambient filter restricts it by ``id`` instead.
_TENANT_ROOT_TABLE: Final = "hospitals"


class TenantScopeRequiredError(RuntimeError):
    """A tenant-scoped operation was attempted with no tenant.

    Raised instead of running an unrestricted query. It is a programming
    error, not a client error, so it is not mapped to a 4xx.
    """


class CrossTenantAccessError(RuntimeError):
    """An operation tried to write a row that belongs to another hospital."""


@dataclass(frozen=True, slots=True)
class CrossTenant:
    """An explicit, searchable request for a cross-hospital repository read.

    Build it with :func:`cross_tenant`. It exists so that every legitimate
    cross-tenant lookup is a visible decision in the code with its reason next
    to it, rather than the default behaviour of a forgotten argument.

    It satisfies Layer 1 only. It does **not** lift an ambient hospital scope:
    inside a staff request the session is still confined to that staff
    member's hospital.

    :param reason: Why this lookup may cross hospitals.
    """

    reason: str


def cross_tenant(reason: str) -> CrossTenant:
    """Mark a repository call as a deliberate cross-hospital lookup.

    :param reason: Why the lookup may cross hospitals. Required and non-empty,
        so the call site documents itself.
    :returns: The marker to pass as the repository scope.
    :raises ValueError: If no reason is given.
    """
    if not reason or not reason.strip():
        msg = "cross_tenant() needs a reason."
        raise ValueError(msg)
    return CrossTenant(reason=reason)


def tenant_or_platform(hospital_id: uuid.UUID | None, *, reason: str) -> uuid.UUID | CrossTenant:
    """Turn an actor's hospital into a repository scope.

    Several services take ``actor_hospital_id`` and document ``None`` as "a
    platform-level actor with no hospital" (Super Admin, internal caller).
    This keeps that convention but makes the unconstrained branch explicit.

    :param hospital_id: The actor's hospital, or ``None`` for a platform actor.
    :param reason: Why the platform branch may cross hospitals.
    :returns: The hospital id, or a :class:`CrossTenant` marker.
    """
    return hospital_id if hospital_id is not None else cross_tenant(reason)


@dataclass(frozen=True, slots=True)
class TenantScope:
    """The tenant the current task is confined to.

    :param hospital_id: The hospital, or ``None`` for a system scope.
    :param reason: For a system scope, why it is not confined to one hospital.
    """

    hospital_id: uuid.UUID | None
    reason: str | None = None

    @classmethod
    def hospital(cls, hospital_id: uuid.UUID) -> TenantScope:
        """Confine the task to one hospital.

        :param hospital_id: The trusted hospital id.
        :raises TenantScopeRequiredError: If ``hospital_id`` is ``None``.
        """
        if hospital_id is None:
            msg = "TenantScope.hospital() needs a hospital id."
            raise TenantScopeRequiredError(msg)
        return cls(hospital_id=hospital_id)

    @classmethod
    def system(cls, reason: str) -> TenantScope:
        """An explicit scope that is not confined to one hospital.

        :param reason: Why this work legitimately spans hospitals.
        :raises ValueError: If no reason is given.
        """
        if not reason or not reason.strip():
            msg = "TenantScope.system() needs a reason."
            raise ValueError(msg)
        return cls(hospital_id=None, reason=reason)

    @property
    def is_system(self) -> bool:
        """True if this scope spans hospitals."""
        return self.hospital_id is None


_current_scope: ContextVar[TenantScope | None] = ContextVar("aetheris_tenant_scope", default=None)


def current_tenant_scope() -> TenantScope | None:
    """Return the scope bound for the current task, or ``None``."""
    return _current_scope.get()


def bind_tenant_scope(scope: TenantScope) -> None:
    """Bind ``scope`` for the rest of the current task.

    For request dependencies, which cannot wrap the endpoint in a ``with``
    block. Each request runs in its own task with its own copy of the context,
    so the binding ends with the request and is never seen by another one.
    Everything else should prefer :func:`tenant_scope`.

    :param scope: The scope to bind.
    """
    _current_scope.set(scope)


@contextmanager
def tenant_scope(scope: TenantScope) -> Iterator[TenantScope]:
    """Bind ``scope`` for the duration of a block, then restore the previous one.

    :param scope: The scope to bind.
    """
    token = _current_scope.set(scope)
    try:
        yield scope
    finally:
        _current_scope.reset(token)


def scope_for_principal(hospital_id: uuid.UUID | None) -> TenantScope:
    """Return the scope for an authenticated principal.

    :param hospital_id: The principal's hospital. ``None`` only for a
        platform-level principal (Super Admin).
    :returns: A hospital scope, or a system scope for a platform principal.
    """
    if hospital_id is None:
        return TenantScope.system("platform-level principal (no hospital)")
    return TenantScope.hospital(hospital_id)


# ── Model classification ─────────────────────────────────────────────────────


def is_tenant_scoped(model: type[Any]) -> bool:
    """Whether ``model`` is a tenant-scoped table (it has a ``hospital_id`` column).

    :param model: A mapped class.
    """
    table = getattr(model, "__table__", None)
    return table is not None and TENANT_COLUMN in table.c


def tenant_column_nullable(model: type[Any]) -> bool:
    """Whether a tenant-scoped model allows rows with no hospital.

    ``roles`` (system roles shared by every hospital) and ``audit_logs``
    (platform events) do. Such a row belongs to no hospital, so it is not
    another hospital's row.

    :param model: A tenant-scoped mapped class.
    """
    return bool(model.__table__.c[TENANT_COLUMN].nullable)


def _ambient_criteria(model: type[Any], hospital_id: uuid.UUID) -> ColumnElement[bool] | None:
    """Build the ambient ``WHERE`` criteria for one entity, or ``None``."""
    table = getattr(model, "__table__", None)
    if table is None:
        return None
    if table.name == _TENANT_ROOT_TABLE:
        return model.id == hospital_id  # type: ignore[no-any-return]
    if TENANT_COLUMN not in table.c:
        return None
    column = getattr(model, TENANT_COLUMN)
    if table.c[TENANT_COLUMN].nullable:
        return or_(column == hospital_id, column.is_(None))
    return column == hospital_id  # type: ignore[no-any-return]


# ── Session guards (Layer 2) ─────────────────────────────────────────────────


def _restrict_statement(state: ORMExecuteState) -> None:
    """Confine an ORM statement to the bound hospital.

    Applies to SELECT, and to ORM-enabled UPDATE and DELETE. Runs for
    relationship and column loads too: adding the same criteria twice is
    harmless, and it covers a related tenant entity that was not part of the
    statement that loaded its parent.
    """
    scope = _current_scope.get()
    if scope is None or scope.hospital_id is None:
        return
    if not (state.is_select or state.is_update or state.is_delete):
        return

    hospital_id = scope.hospital_id
    statement = state.statement
    for model in _entities_in(state):
        criteria = _ambient_criteria(model, hospital_id)
        if criteria is not None:
            statement = statement.options(
                with_loader_criteria(model, criteria, include_aliases=True)
            )
    state.statement = statement


def _entities_in(state: ORMExecuteState) -> list[type[Any]]:
    """Return every mapped class a statement reads, including inside subqueries.

    ``all_mappers`` lists only the top-level entities. A count wrapped around
    a pre-built subquery — ``select(count()).select_from(stmt.subquery())``,
    the shape every paginated list uses — has none, so the statement is also
    walked for the ORM annotations its inner tables and columns carry.
    """
    found: dict[type[Any], None] = {mapper.class_: None for mapper in state.all_mappers}
    for element in visitors.iterate(cast("ExternallyTraversible", state.statement)):
        annotations = getattr(element, "_annotations", None)
        if not annotations:
            continue
        entity = annotations.get("parententity")
        mapper = getattr(entity, "mapper", None)
        if mapper is not None:
            found.setdefault(mapper.class_, None)
    return list(found)


def _refuse_cross_tenant_flush(session: Session, _context: Any, _instances: Any) -> None:
    """Refuse a flush that would write another hospital's row.

    Two checks:

    * The tenant column of a persistent row never changes — in any scope. A
      row cannot be moved between hospitals.
    * Under a hospital scope, every new, changed or deleted tenant-scoped row
      must belong to that hospital (or to no hospital, where the column is
      nullable).
    """
    scope = _current_scope.get()
    hospital_id = scope.hospital_id if scope is not None else None

    for instance in (*session.new, *session.dirty, *session.deleted):
        model = type(instance)
        if not is_tenant_scoped(model):
            continue

        state = inspect(instance)
        if state.persistent:
            history = state.attrs[TENANT_COLUMN].history
            if history.deleted and history.added and history.deleted != history.added:
                msg = f"{model.__name__}.{TENANT_COLUMN} cannot be changed on an existing row."
                raise CrossTenantAccessError(msg)

        if hospital_id is None:
            continue
        row_hospital = getattr(instance, TENANT_COLUMN)
        if row_hospital is not None and row_hospital != hospital_id:
            msg = f"Refusing to write a {model.__name__} row that belongs to another hospital."
            raise CrossTenantAccessError(msg)


_guards_installed = False


def install_tenant_guards() -> None:
    """Register the Layer 2 session listeners. Idempotent.

    Registered on :class:`sqlalchemy.orm.Session` itself, so it covers every
    session in the process — request sessions, worker sessions and test
    sessions alike. With no scope bound the listeners return immediately.
    """
    global _guards_installed  # noqa: PLW0603 — one-time process-wide registration
    if _guards_installed:
        return
    event.listen(Session, "do_orm_execute", _restrict_statement)
    event.listen(Session, "before_flush", _refuse_cross_tenant_flush)
    _guards_installed = True
