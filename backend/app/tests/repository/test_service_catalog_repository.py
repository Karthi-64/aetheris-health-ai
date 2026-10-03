"""Repository tests for the services catalog.

Real Postgres, rolled back per test. Every read method is checked for tenant
isolation (``backend/CLAUDE.md``: "every repository method has at least one
test that verifies ``hospital_id`` filtering").
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.repositories.service_catalog_repository import ServiceCatalogRepository

if TYPE_CHECKING:
    import uuid

    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.billing import Service

pytestmark = pytest.mark.database


@pytest.fixture
def repository(db_session: AsyncSession) -> ServiceCatalogRepository:
    """A repository bound to the rolled-back test session."""
    return ServiceCatalogRepository(db_session)


async def _add(
    repository: ServiceCatalogRepository, hospital_id: uuid.UUID, code: str, **fields: Any
) -> Service:
    """Insert a catalog service with sensible defaults."""
    values: dict[str, Any] = {"name": code.title(), "price": Decimal("100.00")}
    values.update(fields)
    return await repository.create_service(hospital_id=hospital_id, code=code, **values)


class TestCreate:
    async def test_persists_with_defaults(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        service = await _add(
            repository, hospital_id, "ECG", price=Decimal("450.00"), created_by=actor_id
        )

        assert service.id is not None
        assert service.price == Decimal("450.00")
        assert service.taxable is True
        assert service.is_active is True
        assert service.created_by == actor_id

    async def test_code_is_unique_per_hospital(
        self, repository: ServiceCatalogRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        await _add(repository, hospital_id, "ECG")

        with pytest.raises(IntegrityError, match="uq_services_hospital_code"):
            async with db_session.begin_nested():
                await _add(repository, hospital_id, "ECG")

    async def test_two_hospitals_may_share_a_code(
        self,
        repository: ServiceCatalogRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        ours = await _add(repository, hospital_id, "ECG")
        theirs = await _add(repository, other_hospital_id, "ECG")

        assert ours.id != theirs.id

    async def test_database_refuses_a_negative_price(
        self, repository: ServiceCatalogRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        with pytest.raises(IntegrityError, match="price_non_negative"):
            async with db_session.begin_nested():
                await _add(repository, hospital_id, "BAD", price=Decimal("-1.00"))


class TestUpdate:
    async def test_update_changes_fields_and_stamps_the_actor(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID, actor_id: uuid.UUID
    ) -> None:
        service = await _add(repository, hospital_id, "ECG")

        updated = await repository.update_service(
            service, updated_by=actor_id, price=Decimal("475.50"), is_active=False
        )

        assert updated.price == Decimal("475.50")
        assert updated.is_active is False
        assert updated.updated_by == actor_id


class TestReadsAreTenantScoped:
    async def test_get_by_id(
        self,
        repository: ServiceCatalogRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        service = await _add(repository, hospital_id, "ECG")

        assert await repository.get_service_by_id(hospital_id, service.id) is not None
        assert await repository.get_service_by_id(other_hospital_id, service.id) is None

    async def test_get_by_ids_drops_another_tenants_services(
        self,
        repository: ServiceCatalogRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        ours = await _add(repository, hospital_id, "ECG")
        theirs = await _add(repository, other_hospital_id, "XRAY")

        found = await repository.get_services_by_ids(hospital_id, [ours.id, theirs.id])

        assert [service.id for service in found] == [ours.id]

    async def test_get_by_ids_with_nothing_asked_for(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        assert await repository.get_services_by_ids(hospital_id, []) == []

    async def test_get_by_code(
        self,
        repository: ServiceCatalogRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await _add(repository, hospital_id, "ECG")

        assert await repository.get_service_by_code(hospital_id, "ECG") is not None
        assert await repository.get_service_by_code(other_hospital_id, "ECG") is None

    async def test_list_and_count(
        self,
        repository: ServiceCatalogRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        await _add(repository, hospital_id, "ECG")
        await _add(repository, hospital_id, "XRAY")
        await _add(repository, other_hospital_id, "MRI")

        listed = await repository.list_services(hospital_id)

        assert {service.code for service in listed} == {"ECG", "XRAY"}
        assert await repository.count_services(hospital_id) == 2
        assert await repository.count_services(other_hospital_id) == 1


class TestListFilters:
    @pytest.fixture
    async def catalog(self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID) -> None:
        """Four services across two categories, one of them retired."""
        await _add(
            repository, hospital_id, "CONS-GEN", name="Consultation", category="Consultation"
        )
        await _add(
            repository,
            hospital_id,
            "CONS-FU",
            name="Consultation follow-up",
            category="Consultation",
        )
        await _add(repository, hospital_id, "ECG", name="ECG", category="Diagnostics")
        await _add(
            repository,
            hospital_id,
            "OLD",
            name="Retired test",
            category="Diagnostics",
            is_active=False,
        )

    @pytest.mark.usefixtures("catalog")
    async def test_orders_by_name(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        names = [service.name for service in await repository.list_services(hospital_id)]

        assert names == ["Consultation", "Consultation follow-up", "ECG", "Retired test"]

    @pytest.mark.usefixtures("catalog")
    async def test_term_matches_a_name_prefix_case_insensitively(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        found = await repository.list_services(hospital_id, term="consult")

        assert {service.code for service in found} == {"CONS-GEN", "CONS-FU"}
        assert await repository.count_services(hospital_id, term="consult") == 2

    @pytest.mark.usefixtures("catalog")
    async def test_term_matches_an_exact_code(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        found = await repository.list_services(hospital_id, term="cons-fu")

        assert [service.code for service in found] == ["CONS-FU"]

    @pytest.mark.usefixtures("catalog")
    async def test_term_is_not_a_substring_match(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        assert await repository.list_services(hospital_id, term="follow") == []

    @pytest.mark.usefixtures("catalog")
    async def test_wildcards_in_the_term_are_literal(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        assert await repository.list_services(hospital_id, term="%") == []
        assert await repository.list_services(hospital_id, term="_CG") == []

    @pytest.mark.usefixtures("catalog")
    async def test_category_and_active_filters(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        diagnostics = await repository.list_services(hospital_id, category="Diagnostics")
        billable = await repository.list_services(
            hospital_id, category="Diagnostics", is_active=True
        )
        retired = await repository.list_services(hospital_id, is_active=False)

        assert {service.code for service in diagnostics} == {"ECG", "OLD"}
        assert [service.code for service in billable] == ["ECG"]
        assert [service.code for service in retired] == ["OLD"]

    @pytest.mark.usefixtures("catalog")
    async def test_pagination(
        self, repository: ServiceCatalogRepository, hospital_id: uuid.UUID
    ) -> None:
        first = await repository.list_services(hospital_id, skip=0, limit=3)
        second = await repository.list_services(hospital_id, skip=3, limit=3)

        assert len(first) == 3
        assert [service.code for service in second] == ["OLD"]
