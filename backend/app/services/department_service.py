"""Business logic for the Department module.

Owns every department business rule from
``docs/modules/14-hospital-settings.md`` §4 (rules 11–14), the transaction
boundary for department writes (``docs/03-ARCHITECTURE.md`` §9), and the audit
record for every mutation (CLAUDE.md rule 9).

Returns Pydantic DTOs, never ORM models (``docs/03-ARCHITECTURE.md`` §15,
rule 7).

**Tenancy.** Every public method takes ``hospital_id`` and passes it to the
repository. A caller that supplies the wrong one gets nothing back rather than
another tenant's data, because the filter is applied in SQL.

**The doctors seam.** Rule 13 forbids deactivating a department that still has
active doctors assigned, but the Doctor Management module does not exist yet.
:class:`DepartmentUsageSource` is the interface that answers "how many active
doctors are assigned to this department"; :class:`NullDepartmentUsageSource`
answers zero until Doctor Management supplies a real implementation. The guard
and its tests ship now, so the Doctors PR swaps one DI provider and changes no
service code. This mirrors how :class:`~app.core.audit.AuditSink` stands in for
the not-yet-built Audit Logs module.
"""

from __future__ import annotations

import re
import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import BusinessRuleError, ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.department import CODE_PATTERN
from app.schemas.common import Page, PaginationParams
from app.schemas.department import (
    CreateDepartmentRequest,
    DepartmentResponse,
    DepartmentSummaryResponse,
    SearchDepartmentRequest,
    UpdateDepartmentRequest,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.department import Department
    from app.repositories.department_repository import DepartmentRepository

logger = get_logger(__name__)

__all__ = [
    "DepartmentAlreadyActiveError",
    "DepartmentInUseError",
    "DepartmentNotDeactivatedError",
    "DepartmentNotFoundError",
    "DepartmentService",
    "DepartmentUsageSource",
    "DuplicateDepartmentCodeError",
    "DuplicateDepartmentNameError",
    "NullDepartmentUsageSource",
]

#: Names of the database constraints that enforce per-hospital uniqueness.
#: Checked against the driver error so a race between two concurrent creates
#: surfaces as a 409 naming the field, not a 500.
_CODE_UNIQUE_CONSTRAINT = "uq_departments_hospital_code"
_NAME_UNIQUE_CONSTRAINT = "uq_departments_hospital_name_lower"

#: Column names a client may set through create/update. Anything outside this
#: set is rejected before it reaches the repository, so a schema change that
#: accidentally widens a DTO cannot silently widen what is writable.
_WRITABLE_COLUMNS = frozenset(
    {
        "code",
        "name",
        "description",
        "phone_extension",
        "email",
        "location",
    }
)


# ── The doctors seam ────────────────────────────────────────────────────────


@runtime_checkable
class DepartmentUsageSource(Protocol):
    """Answers how many active doctors are assigned to a department.

    Implemented today by :class:`NullDepartmentUsageSource` and, once
    ``docs/modules/04-doctor-management.md`` ships, by a
    ``DoctorRepository``-backed adapter. The department service depends on this
    protocol rather than on Doctor Management directly, which keeps the
    dependency pointing one way: Doctors knows about Departments, not the
    reverse.
    """

    async def active_doctor_count(self, hospital_id: uuid.UUID, department_id: uuid.UUID) -> int:
        """Count active (non-deactivated) doctors assigned to a department.

        :param hospital_id: The tenant to scope to.
        :param department_id: The department being checked.
        :returns: The number of active doctors assigned.
        """
        ...


class NullDepartmentUsageSource:
    """Interim :class:`DepartmentUsageSource` that reports no assignments.

    Correct, not merely convenient: no ``doctors`` table exists, so zero
    doctors really are assigned to every department. When Doctor Management
    lands, its adapter replaces this one in
    ``app/api/dependencies/services.py`` and rule 13 begins to bite with no
    change to :class:`DepartmentService`.
    """

    async def active_doctor_count(self, hospital_id: uuid.UUID, department_id: uuid.UUID) -> int:
        """Return zero — nothing can be assigned to a department yet.

        :param hospital_id: Unused; present to satisfy the protocol.
        :param department_id: Unused; present to satisfy the protocol.
        :returns: Always ``0``.
        """
        return 0


# ── Module exceptions ───────────────────────────────────────────────────────
# docs/03-ARCHITECTURE.md §10: business exceptions belong to the module that
# raises them. They subclass the shared hierarchy in app/core/exceptions.py so
# the global handler maps them to the right status and error code with no
# per-module wiring.


class DepartmentNotFoundError(NotFoundError):
    """Raised when a department does not exist in the requested hospital.

    Also raised when the department exists in a *different* hospital: a
    cross-tenant lookup must be indistinguishable from a miss, or the 404/403
    difference leaks the existence of another tenant's records.
    """

    def __init__(self, department_id: uuid.UUID) -> None:
        super().__init__(
            message="Department not found.",
            detail={"department_id": str(department_id)},
        )


class DuplicateDepartmentCodeError(ConflictError):
    """Raised when a department code is already in use within the hospital."""

    def __init__(self, code: str | None = None) -> None:
        super().__init__(
            message="A department with this code already exists.",
            detail={"code": code} if code else None,
        )


class DuplicateDepartmentNameError(ConflictError):
    """Raised when a department name is already in use within the hospital.

    Names are compared case-insensitively, so this fires for "Cardiology" when
    "cardiology" already exists.
    """

    def __init__(self, name: str | None = None) -> None:
        super().__init__(
            message="A department with this name already exists.",
            detail={"name": name} if name else None,
        )


class DepartmentInUseError(ConflictError):
    """Raised when deactivating a department that still has active doctors.

    Module spec §4, rule 13. The count is included so the caller can tell the
    user how much reassignment work stands between them and the deletion.
    """

    def __init__(self, department_id: uuid.UUID, assigned_doctors: int) -> None:
        super().__init__(
            message=(
                "Department cannot be deactivated while doctors are assigned to it. "
                "Reassign or deactivate them first."
            ),
            detail={
                "department_id": str(department_id),
                "assigned_doctors": assigned_doctors,
            },
        )


class DepartmentAlreadyActiveError(BusinessRuleError):
    """Raised when reactivating a department that was never deactivated."""

    def __init__(self, department_id: uuid.UUID) -> None:
        super().__init__(
            message="Department is already active.",
            detail={"department_id": str(department_id)},
        )


class DepartmentNotDeactivatedError(BusinessRuleError):
    """Raised when deactivating a department that is already deactivated."""

    def __init__(self, department_id: uuid.UUID) -> None:
        super().__init__(
            message="Department is already deactivated.",
            detail={"department_id": str(department_id)},
        )


class DepartmentService:
    """Department creation, retrieval, search, and lifecycle.

    :param departments: Department data access.
    :param session: The request-scoped session. Held only to own the
        transaction boundary — the service never queries through it directly
        (``docs/03-ARCHITECTURE.md`` §15, rule 3).
    :param audit: Where audit events are recorded.
    :param usage: Answers how many doctors are assigned to a department.
    """

    def __init__(
        self,
        departments: DepartmentRepository,
        session: AsyncSession,
        audit: AuditSink,
        usage: DepartmentUsageSource,
    ) -> None:
        self._departments = departments
        self._session = session
        self._audit = audit
        self._usage = usage

    # ── Validation ────────────────────────────────────────────────────────────

    def validate_department_data(
        self, payload: CreateDepartmentRequest | UpdateDepartmentRequest
    ) -> None:
        """Assert the module's business rules over a create or update payload.

        The Pydantic schemas already enforce these at the API boundary. They are
        re-asserted here because the service is also reachable from seed
        scripts, background jobs, and other services, none of which go through
        the HTTP layer. A rule that only holds for HTTP callers is not a rule.

        Checks (module spec §11):

        - code, when present, matches the required format after uppercasing
        - name, when present, is non-blank and within length bounds

        :param payload: The create or update payload to validate.
        :raises ValidationError: If any rule is violated. ``detail`` carries
            per-field messages in the shape ``docs/06-API_STANDARDS.md`` §5.3
            expects.
        """
        errors: list[dict[str, str]] = []

        code = payload.code
        if code is not None and not re.match(CODE_PATTERN, code):
            errors.append(
                {
                    "field": "code",
                    "message": (
                        "Must be 2–20 characters of uppercase letters, digits, "
                        "hyphens, or underscores."
                    ),
                }
            )

        name = payload.name
        if name is not None:
            stripped = name.strip()
            if len(stripped) < 2:
                errors.append(
                    {"field": "name", "message": "Must be at least 2 non-whitespace characters."}
                )
            elif len(stripped) > 150:
                errors.append({"field": "name", "message": "Must be at most 150 characters."})

        if errors:
            logger.warning(
                "department.validation_failed",
                fields=[error["field"] for error in errors],
            )
            raise ValidationError(
                message="Department data failed validation.",
                detail={"errors": errors},
            )

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_department(
        self,
        hospital_id: uuid.UUID,
        payload: CreateDepartmentRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DepartmentResponse:
        """Create a department (module spec §9).

        Duplicate ``code`` and ``name`` are checked up front so the common case
        returns a clear 409, and again via the database constraint so a race
        between two concurrent creates cannot produce two rows.

        :param hospital_id: The hospital the department belongs to.
        :param payload: Validated creation data.
        :param actor_id: UUID of the acting user.
        :returns: The created department.
        :raises ValidationError: If the payload violates a business rule.
        :raises DuplicateDepartmentCodeError: If the code is taken.
        :raises DuplicateDepartmentNameError: If the name is taken.
        """
        self.validate_department_data(payload)
        await self._assert_code_available(hospital_id, payload.code)
        await self._assert_name_available(hospital_id, payload.name)

        values = self._writable_values(payload.model_dump(mode="json"))

        try:
            async with self._session.begin_nested():
                department = await self._departments.create_department(
                    hospital_id=hospital_id,
                    created_by=actor_id,
                    **values,
                )
        except IntegrityError as exc:
            # Lost a race with a concurrent create. Translate the constraint
            # violation into the same 409 the up-front check would have raised.
            self._raise_for_unique_violation(exc, code=payload.code, name=payload.name)
            raise

        await self._audit.record(
            AuditEvent(
                action="department.created",
                hospital_id=hospital_id,
                target_type="department",
                target_id=department.id,
                actor_id=actor_id,
                changes={name: {"before": None, "after": value} for name, value in values.items()},
            )
        )
        await self._session.commit()

        logger.info(
            "department.created",
            hospital_id=str(hospital_id),
            department_id=str(department.id),
            code=department.code,
            actor_id=str(actor_id) if actor_id else None,
        )
        return DepartmentResponse.from_model(department)

    async def update_department(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        payload: UpdateDepartmentRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DepartmentResponse:
        """Apply a partial update to a department (module spec §9).

        Only fields the client actually sent are applied — an absent field and
        an explicit ``null`` are different requests, and conflating them would
        silently wipe data the client never mentioned.

        :param hospital_id: The hospital the department belongs to.
        :param department_id: The department to update.
        :param payload: Partial update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated department.
        :raises DepartmentNotFoundError: If the department is absent from this tenant.
        :raises ValidationError: If the payload violates a business rule.
        :raises DuplicateDepartmentCodeError: If the new code is taken.
        :raises DuplicateDepartmentNameError: If the new name is taken.
        """
        self.validate_department_data(payload)

        department = await self._get_or_raise(hospital_id, department_id)

        changes = self._writable_values(payload.changed_fields())

        # Uniqueness only needs rechecking when the value actually moves.
        # Re-submitting a department's own code must not collide with itself.
        if "code" in changes and changes["code"] != department.code:
            await self._assert_code_available(hospital_id, changes["code"])
        if "name" in changes and changes["name"].lower() != department.name.lower():
            await self._assert_name_available(hospital_id, changes["name"])

        diff = self._diff(department, changes)
        if not diff:
            # Nothing actually changed. Return the current state rather than
            # writing a no-op row and an audit entry that implies an edit.
            logger.info(
                "department.update_noop",
                hospital_id=str(hospital_id),
                department_id=str(department_id),
            )
            return DepartmentResponse.from_model(department)

        try:
            async with self._session.begin_nested():
                department = await self._departments.update_department(
                    department, updated_by=actor_id, **changes
                )
        except IntegrityError as exc:
            self._raise_for_unique_violation(
                exc, code=changes.get("code"), name=changes.get("name")
            )
            raise

        await self._audit.record(
            AuditEvent(
                action="department.updated",
                hospital_id=hospital_id,
                target_type="department",
                target_id=department.id,
                actor_id=actor_id,
                changes=diff,
            )
        )
        await self._session.commit()

        logger.info(
            "department.updated",
            hospital_id=str(hospital_id),
            department_id=str(department.id),
            actor_id=str(actor_id) if actor_id else None,
            changed_fields=sorted(diff),
        )
        return DepartmentResponse.from_model(department)

    async def deactivate_department(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DepartmentResponse:
        """Deactivate a department by soft-deleting the record.

        This is what ``DELETE /api/v1/departments/{id}`` performs. Departments
        are never removed from the database (module spec §4, rule 12) —
        doctors and appointments keep referencing them.

        Rule 13 is enforced here: a department with active doctors assigned
        cannot be deactivated.

        :param hospital_id: The hospital the department belongs to.
        :param department_id: The department to deactivate.
        :param actor_id: UUID of the acting user.
        :returns: The deactivated department.
        :raises DepartmentNotFoundError: If the department is absent from this tenant.
        :raises DepartmentNotDeactivatedError: If already deactivated.
        :raises DepartmentInUseError: If active doctors are still assigned.
        """
        department = await self._departments.get_department_by_id(
            hospital_id, department_id, include_deleted=True
        )
        if department is None:
            raise DepartmentNotFoundError(department_id)
        if department.deleted_at is not None:
            raise DepartmentNotDeactivatedError(department_id)

        assigned = await self._usage.active_doctor_count(hospital_id, department_id)
        if assigned > 0:
            logger.info(
                "department.deactivate_blocked",
                hospital_id=str(hospital_id),
                department_id=str(department_id),
                assigned_doctors=assigned,
            )
            raise DepartmentInUseError(department_id, assigned)

        department = await self._departments.delete_department(department, deleted_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="department.deactivated",
                hospital_id=hospital_id,
                target_type="department",
                target_id=department.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info(
            "department.deactivated",
            hospital_id=str(hospital_id),
            department_id=str(department.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return DepartmentResponse.from_model(department)

    async def activate_department(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> DepartmentResponse:
        """Reactivate a previously deactivated department.

        :param hospital_id: The hospital the department belongs to.
        :param department_id: The department to reactivate.
        :param actor_id: UUID of the acting user.
        :returns: The reactivated department.
        :raises DepartmentNotFoundError: If the department is absent from this tenant.
        :raises DepartmentAlreadyActiveError: If it was never deactivated.
        """
        department = await self._departments.get_department_by_id(
            hospital_id, department_id, include_deleted=True
        )
        if department is None:
            raise DepartmentNotFoundError(department_id)
        if department.deleted_at is None:
            raise DepartmentAlreadyActiveError(department_id)

        department = await self._departments.restore_department(department, updated_by=actor_id)

        await self._audit.record(
            AuditEvent(
                action="department.activated",
                hospital_id=hospital_id,
                target_type="department",
                target_id=department.id,
                actor_id=actor_id,
            )
        )
        await self._session.commit()

        logger.info(
            "department.activated",
            hospital_id=str(hospital_id),
            department_id=str(department.id),
            actor_id=str(actor_id) if actor_id else None,
        )
        return DepartmentResponse.from_model(department)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_department_details(
        self,
        hospital_id: uuid.UUID,
        department_id: uuid.UUID,
        *,
        include_inactive: bool = False,
    ) -> DepartmentResponse:
        """Retrieve one department's full record.

        :param hospital_id: The hospital the department belongs to.
        :param department_id: The department UUID.
        :param include_inactive: Return the record even if deactivated.
        :returns: The department.
        :raises DepartmentNotFoundError: If the department is absent from this tenant.
        """
        department = await self._departments.get_department_by_id(
            hospital_id, department_id, include_deleted=include_inactive
        )
        if department is None:
            raise DepartmentNotFoundError(department_id)
        return DepartmentResponse.from_model(department)

    async def list_departments(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        include_inactive: bool = False,
    ) -> Page[DepartmentSummaryResponse]:
        """List departments in a hospital, ordered by name.

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param include_inactive: Include deactivated departments.
        :returns: One page of department summaries plus the total count.
        """
        page_params = pagination or PaginationParams()

        rows = await self._departments.list_departments(
            hospital_id,
            include_deleted=include_inactive,
            skip=page_params.offset,
            limit=page_params.limit,
        )
        total = await self._departments.count_departments(
            hospital_id,
            include_deleted=include_inactive,
        )

        return Page[DepartmentSummaryResponse](
            items=[DepartmentSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    async def search_departments(
        self,
        hospital_id: uuid.UUID,
        filters: SearchDepartmentRequest,
        *,
        pagination: PaginationParams | None = None,
        actor_id: uuid.UUID | None = None,
    ) -> Page[DepartmentSummaryResponse]:
        """Search departments within a hospital.

        ``q`` prefix-matches name case-insensitively and exact-matches code.

        :param hospital_id: The hospital to search.
        :param filters: Search term and filters.
        :param pagination: Page and page size. Defaults to page 1.
        :param actor_id: UUID of the acting user, recorded on the search audit
            event.
        :returns: One page of matching summaries plus the total match count.
        """
        page_params = pagination or PaginationParams()

        criteria: dict[str, Any] = {
            "term": filters.q,
            "include_deleted": filters.include_inactive,
        }

        rows = await self._departments.search_departments(
            hospital_id,
            skip=page_params.offset,
            limit=page_params.limit,
            **criteria,
        )
        total = await self._departments.count_departments(hospital_id, **criteria)

        await self._audit.record(
            AuditEvent(
                action="department.searched",
                hospital_id=hospital_id,
                target_type="department",
                actor_id=actor_id,
                context={
                    "filters_used": sorted(
                        name for name, value in criteria.items() if value not in (None, False)
                    ),
                    "result_count": total,
                },
            )
        )

        logger.info(
            "department.searched",
            hospital_id=str(hospital_id),
            actor_id=str(actor_id) if actor_id else None,
            result_count=total,
        )
        # Deliberately no commit: a read path must not commit a shared
        # transaction. The search event reaches structlog via the sink; the
        # durable trail carries mutations only.

        return Page[DepartmentSummaryResponse](
            items=[DepartmentSummaryResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(self, hospital_id: uuid.UUID, department_id: uuid.UUID) -> Department:
        """Fetch an active department or raise :class:`DepartmentNotFoundError`."""
        department = await self._departments.get_department_by_id(hospital_id, department_id)
        if department is None:
            raise DepartmentNotFoundError(department_id)
        return department

    async def _assert_code_available(self, hospital_id: uuid.UUID, code: str | None) -> None:
        """Raise if ``code`` is already taken in this hospital.

        Deactivated departments count as taking their code: the unique
        constraint applies regardless of soft-delete state, so ignoring them
        would report "available" for a code the database will reject.
        """
        if code is None:
            return
        existing = await self._departments.get_department_by_code(hospital_id, code)
        if existing is not None:
            raise DuplicateDepartmentCodeError(code)

    async def _assert_name_available(self, hospital_id: uuid.UUID, name: str | None) -> None:
        """Raise if ``name`` is already taken in this hospital, case-insensitively."""
        if name is None:
            return
        existing = await self._departments.get_department_by_name(hospital_id, name)
        if existing is not None:
            raise DuplicateDepartmentNameError(name)

    @staticmethod
    def _raise_for_unique_violation(
        exc: IntegrityError, *, code: str | None, name: str | None
    ) -> None:
        """Translate a unique-constraint violation into the matching 409.

        Any other integrity error (a bad ``hospital_id`` foreign key, the code
        check constraint) is a real bug and is left to propagate.

        :param exc: The raised integrity error.
        :param code: The code that was being written, for the error detail.
        :param name: The name that was being written, for the error detail.
        """
        driver_error = getattr(exc, "orig", None)
        # asyncpg surfaces the constraint name on the wrapped exception; the
        # string check is the fallback for drivers that do not.
        constraint_name = getattr(getattr(driver_error, "__cause__", None), "constraint_name", None)
        blob = f"{constraint_name or ''} {driver_error}"

        if _CODE_UNIQUE_CONSTRAINT in blob:
            raise DuplicateDepartmentCodeError(code) from exc
        if _NAME_UNIQUE_CONSTRAINT in blob:
            raise DuplicateDepartmentNameError(name) from exc

    @staticmethod
    def _writable_values(values: dict[str, Any]) -> dict[str, Any]:
        """Keep only the columns a client is allowed to write.

        :param values: Candidate column values.
        :returns: The subset present in :data:`_WRITABLE_COLUMNS`.
        """
        return {name: value for name, value in values.items() if name in _WRITABLE_COLUMNS}

    @staticmethod
    def _diff(department: Department, changes: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """Build a before/after diff, dropping fields whose value is unchanged.

        :param department: The department as it stands before the update.
        :param changes: Proposed column values.
        :returns: ``{field: {"before": old, "after": new}}`` for real changes.
        """
        diff: dict[str, dict[str, Any]] = {}
        for name, new_value in changes.items():
            current = getattr(department, name, None)
            if current != new_value:
                diff[name] = {"before": current, "after": new_value}
        return diff
