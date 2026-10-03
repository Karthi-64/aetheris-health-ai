"""Business logic for the billable services catalog.

The catalog half of ``docs/modules/06-billing.md`` (FR-1): what a hospital
charges for, and at what price. Invoices copy a service's name and price onto
their lines at the time of billing, so nothing here can change an invoice that
has already been raised — editing a price affects only future lines.

Returns DTOs, never ORM models, and records an audit event per mutation
(CLAUDE.md rule 9).
"""

from __future__ import annotations

import uuid  # noqa: TC003 — needed at runtime for type hints
from typing import TYPE_CHECKING, Any

from sqlalchemy.exc import IntegrityError

from app.core.audit import AuditEvent
from app.core.exceptions import ConflictError, NotFoundError
from app.core.logging import get_logger
from app.schemas.billing import CreateServiceRequest, ServiceResponse, UpdateServiceRequest
from app.schemas.common import Page, PaginationParams

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.audit import AuditSink
    from app.models.billing import Service
    from app.repositories.service_catalog_repository import ServiceCatalogRepository

logger = get_logger(__name__)

__all__ = [
    "DuplicateServiceCodeError",
    "ServiceCatalogService",
    "ServiceNotFoundError",
]


class ServiceNotFoundError(NotFoundError):
    """Raised when a catalog service is absent from the requested hospital.

    Also raised for one in another tenant: a cross-tenant lookup must be
    indistinguishable from a miss.
    """

    def __init__(self, service_id: uuid.UUID) -> None:
        super().__init__(message="Service not found.", detail={"service_id": str(service_id)})


class DuplicateServiceCodeError(ConflictError):
    """Raised when a catalog code is already in use in the hospital."""

    def __init__(self, code: str) -> None:
        super().__init__(
            message=f"A service with code '{code}' already exists.", detail={"code": code}
        )


def _audit_value(value: Any) -> Any:
    """Render a column value for an audit ``changes`` entry.

    ``Decimal`` is not JSON-serializable, and the durable audit store keeps
    ``changes`` as JSON — so money goes in as its exact string form.
    """
    return value if isinstance(value, bool | str) or value is None else str(value)


class ServiceCatalogService:
    """Create, edit, and read a hospital's billable services.

    :param services: Catalog data access.
    :param session: Request-scoped session, held to own the transaction boundary.
    :param audit: Where audit events are recorded.
    """

    def __init__(
        self,
        services: ServiceCatalogRepository,
        session: AsyncSession,
        audit: AuditSink,
    ) -> None:
        self._services = services
        self._session = session
        self._audit = audit

    # ── Commands ──────────────────────────────────────────────────────────────

    async def create_service(
        self,
        hospital_id: uuid.UUID,
        payload: CreateServiceRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> ServiceResponse:
        """Add a service to the catalog.

        A duplicate code is checked up front so the common case returns a clear
        409, and again via the database constraint so two concurrent creates
        cannot both succeed.

        :param hospital_id: The hospital the service belongs to.
        :param payload: Validated creation data.
        :param actor_id: UUID of the acting user.
        :returns: The created service.
        :raises DuplicateServiceCodeError: If the code is taken.
        """
        if await self._services.get_service_by_code(hospital_id, payload.code) is not None:
            raise DuplicateServiceCodeError(payload.code)

        values = payload.model_dump()
        try:
            async with self._session.begin_nested():
                service = await self._services.create_service(
                    hospital_id=hospital_id, created_by=actor_id, **values
                )
        except IntegrityError as exc:
            if "uq_services_hospital_code" in str(getattr(exc, "orig", exc)):
                raise DuplicateServiceCodeError(payload.code) from exc
            raise

        await self._audit.record(
            AuditEvent(
                action="service.created",
                hospital_id=hospital_id,
                target_type="service",
                target_id=service.id,
                actor_id=actor_id,
                changes={
                    name: {"before": None, "after": _audit_value(value)}
                    for name, value in values.items()
                },
            )
        )
        await self._session.commit()

        logger.info(
            "service.created",
            hospital_id=str(hospital_id),
            service_id=str(service.id),
            code=service.code,
        )
        return ServiceResponse.from_model(service)

    async def update_service(
        self,
        hospital_id: uuid.UUID,
        service_id: uuid.UUID,
        payload: UpdateServiceRequest,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> ServiceResponse:
        """Apply a partial update to a catalog service.

        Only fields the client actually sent are applied. Changing ``price``
        does not touch any existing invoice: lines hold their own copy.

        :param hospital_id: The hospital the service belongs to.
        :param service_id: The service to update.
        :param payload: Validated update data.
        :param actor_id: UUID of the acting user.
        :returns: The updated service.
        :raises ServiceNotFoundError: If absent from this tenant.
        """
        service = await self._get_or_raise(hospital_id, service_id)

        requested = payload.model_dump(exclude_unset=True)
        changes = {
            name: {"before": _audit_value(getattr(service, name)), "after": _audit_value(value)}
            for name, value in requested.items()
            if getattr(service, name) != value
        }
        if not changes:
            # Nothing would change: do not write, and do not record an audit
            # event for an edit that did not happen.
            return ServiceResponse.from_model(service)

        service = await self._services.update_service(
            service, updated_by=actor_id, **{name: requested[name] for name in changes}
        )

        await self._audit.record(
            AuditEvent(
                action="service.updated",
                hospital_id=hospital_id,
                target_type="service",
                target_id=service.id,
                actor_id=actor_id,
                changes=changes,
            )
        )
        await self._session.commit()

        logger.info(
            "service.updated",
            hospital_id=str(hospital_id),
            service_id=str(service.id),
            changed_fields=sorted(changes),
        )
        return ServiceResponse.from_model(service)

    # ── Queries ───────────────────────────────────────────────────────────────

    async def get_service(self, hospital_id: uuid.UUID, service_id: uuid.UUID) -> ServiceResponse:
        """Retrieve one catalog service.

        :raises ServiceNotFoundError: If absent from this tenant.
        """
        return ServiceResponse.from_model(await self._get_or_raise(hospital_id, service_id))

    async def list_services(
        self,
        hospital_id: uuid.UUID,
        *,
        pagination: PaginationParams | None = None,
        term: str | None = None,
        category: str | None = None,
        is_active: bool | None = None,
    ) -> Page[ServiceResponse]:
        """List catalog services (module spec §9).

        :param hospital_id: The hospital to list.
        :param pagination: Page and page size. Defaults to page 1.
        :param term: Name prefix or exact code.
        :param category: Exact category filter.
        :param is_active: Filter on the active flag. ``None`` returns both.
        :returns: One page of services plus the total count.
        """
        page_params = pagination or PaginationParams()
        filters: dict[str, Any] = {"term": term, "category": category, "is_active": is_active}

        rows = await self._services.list_services(
            hospital_id, skip=page_params.offset, limit=page_params.limit, **filters
        )
        total = await self._services.count_services(hospital_id, **filters)

        return Page[ServiceResponse](
            items=[ServiceResponse.from_model(row) for row in rows],
            page=page_params.page,
            page_size=page_params.page_size,
            total_records=total,
        )

    # ── Internals ─────────────────────────────────────────────────────────────

    async def _get_or_raise(self, hospital_id: uuid.UUID, service_id: uuid.UUID) -> Service:
        """Fetch a catalog service or raise :class:`ServiceNotFoundError`."""
        service = await self._services.get_service_by_id(hospital_id, service_id)
        if service is None:
            raise ServiceNotFoundError(service_id)
        return service
