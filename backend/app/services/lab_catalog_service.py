"""Business logic for the lab test catalog.

The catalog half of ``docs/modules/07-laboratory.md`` (FR-1): which tests a
hospital runs, what they cost, and the reference ranges their results are
judged against. Orders copy what they need from a test when it is ordered, so
nothing here can change an order that has already been placed.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import ConflictError, NotFoundError, ValidationError
from app.core.logging import get_logger
from app.models.lab import LabResultType
from app.schemas.common import Page, PaginationParams
from app.schemas.lab import CreateLabTestRequest, LabTestResponse, UpdateLabTestRequest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.lab import LabTest
    from app.repositories.lab_test_repository import LabTestRepository

logger = get_logger(__name__)

__all__ = ["DuplicateLabTestCodeError", "LabCatalogService", "LabTestNotFoundError"]


class LabTestNotFoundError(NotFoundError):
    """Raised when a catalog test is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, test_id: uuid.UUID) -> None:
        super().__init__(message="Lab test not found.", detail={"test_id": str(test_id)})


class DuplicateLabTestCodeError(ConflictError):
    """Raised when a catalog code is already in use in the hospital."""

    def __init__(self, code: str) -> None:
        super().__init__(
            message=f"A lab test with code '{code}' already exists.", detail={"code": code}
        )


def _audit_value(value: Any) -> Any:
    """Render a column value for an audit ``changes`` entry.

    The durable audit store keeps ``changes`` as JSON, so anything that is not
    already JSON-native goes in as its exact string form.
    """
    if isinstance(value, bool | str | int | list) or value is None:
        return value
    return str(value)


class LabCatalogService:
    """Create, edit, and read a hospital's lab tests.

    :param tests: Catalog data access.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(self, tests: LabTestRepository, session: AsyncSession, audit: AuditSink) -> None:
        self._tests = tests
        self._session = session
        self._audit = audit

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_test(
        self,
        hospital_id: uuid.UUID,
        payload: CreateLabTestRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabTestResponse:
        """Add a test to the catalog.

        A duplicate code is checked up front so the common case returns a clear
        409, and again via the database constraint so two concurrent creates
        cannot both succeed.

        :param hospital_id: The hospital the test belongs to.
        :param payload: Validated creation data.
        :param actor_id: UUID of the acting user.
        :returns: The created test.
        :raises DuplicateLabTestCodeError: If the code is taken.
        """
        if await self._tests.get_test_by_code(hospital_id, payload.code) is not None:
            raise DuplicateLabTestCodeError(payload.code)

        # mode="json" so range bounds are stored as exact decimal strings.
        values = payload.model_dump(mode="json")
        values["price"] = payload.price
        values["result_type"] = payload.result_type
        try:
            async with self._session.begin_nested():
                test = await self._tests.create_test(
                    hospital_id=hospital_id, created_by=actor_id, **values
                )
        except IntegrityError as exc:
            if "uq_tests_catalog_hospital_code" in str(getattr(exc, "orig", exc)):
                raise DuplicateLabTestCodeError(payload.code) from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="lab.test.created",
                hospital_id=hospital_id,
                target_type="lab_test",
                target_id=test.id,
                actor_id=actor_id,
                changes={
                    name: {"before": None, "after": _audit_value(value)}
                    for name, value in values.items()
                },
            )
        )
        await self._session.commit()

        logger.info(
            "lab.test.created", hospital_id=str(hospital_id), test_id=str(test.id), code=test.code
        )
        return LabTestResponse.from_model(test)

    async def update_test(
        self,
        hospital_id: uuid.UUID,
        test_id: uuid.UUID,
        payload: UpdateLabTestRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> LabTestResponse:
        """Apply a partial update to a catalog test.

        Only fields the client actually sent are applied. Nothing here touches
        an existing order: items hold their own copies.

        :param hospital_id: The hospital the test belongs to.
        :param test_id: The test to update.
        :param payload: Validated update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated test.
        :raises LabTestNotFoundError: If absent from this tenant.
        :raises ValidationError: If the change would leave a numeric test with
            no reference range, or give a text test one (§11).
        """
        test = await self._get_or_raise(hospital_id, test_id)

        requested = payload.model_dump(exclude_unset=True, mode="json")
        if "price" in requested:
            requested["price"] = payload.price

        if "reference_ranges" in requested:
            message = None
            if test.result_type is LabResultType.NUMERIC and not requested["reference_ranges"]:
                message = "A numeric test needs at least one reference range."
            elif test.result_type is LabResultType.TEXT and requested["reference_ranges"]:
                message = "A text test cannot have reference ranges."
            if message is not None:
                raise ValidationError(
                    message=message,
                    detail={"errors": [{"field": "reference_ranges", "message": message}]},
                )

        changes = {
            name: {"before": _audit_value(getattr(test, name)), "after": _audit_value(value)}
            for name, value in requested.items()
            if getattr(test, name) != value
        }
        if not changes:
            # Nothing would change: do not write, and do not record an audit
            # event for an edit that did not happen.
            return LabTestResponse.from_model(test)

        test = await self._tests.update_test(
            test, updated_by=actor_id, **{name: requested[name] for name in changes}
        )

        await self._audit.record(
            AuditEvent(
                action="lab.test.updated",
                hospital_id=hospital_id,
                target_type="lab_test",
                target_id=test.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._session.commit()

        logger.info(
            "lab.test.updated",
            hospital_id=str(hospital_id),
            test_id=str(test.id),
            changed_fields=sorted(changes),
        )
        return LabTestResponse.from_model(test)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_test(self, hospital_id: uuid.UUID, test_id: uuid.UUID) -> LabTestResponse:
        """Retrieve one catalog test.

        :raises LabTestNotFoundError: If absent from this tenant.
        """
        return LabTestResponse.from_model(await self._get_or_raise(hospital_id, test_id))

    async def list_tests(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Page[LabTestResponse]:
        """List catalog tests.

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param term: Name prefix or exact code.
        :param category: Exact category filter.
        :param is_active: Filter on the active flag. ``None`` returns both.
        :returns: One page of tests plus the total count.
        """
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"term": term, "category": category, "is_active": is_active}

        rows = await self._tests.list_tests(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._tests.count_tests(hospital_id, **filters)

        return Page[LabTestResponse](
            items=[LabTestResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(self, hospital_id: uuid.UUID, test_id: uuid.UUID) -> LabTest:
        """Fetch a catalog test or raise :class:`LabTestNotFoundError`."""
        test = await self._tests.get_test_by_id(hospital_id, test_id)
        if test is None:
            raise LabTestNotFoundError(test_id)
        return test
