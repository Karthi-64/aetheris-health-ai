"""Unit tests for the services catalog service.

Repositories are mocked; no database. The unique-code *constraint* is tested
against real Postgres in the repository suite — here we test that the service
checks first, surfaces a useful 409, and audits only what actually changed.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.exc import IntegrityError

from app.services.service_catalog_service import (
    DuplicateServiceCodeError,
    ServiceCatalogService,
    ServiceNotFoundError,
)
from app.tests.conftest import FakeSession, RecordingAuditSink
from app.tests.factories import (
    build_create_service_request,
    build_service_model,
    build_update_service_request,
)

HOSPITAL_ID = uuid.uuid4()
ACTOR_ID = uuid.uuid4()


def _make_service(repo: AsyncMock) -> tuple[ServiceCatalogService, FakeSession, RecordingAuditSink]:
    """Assemble a service over a mocked repository."""
    session = FakeSession()
    audit = RecordingAuditSink()
    return ServiceCatalogService(repo, session, audit), session, audit  # type: ignore[arg-type]


@pytest.fixture
def repo() -> AsyncMock:
    """A mocked catalog repository with no existing codes."""
    mock = AsyncMock()
    mock.get_service_by_code.return_value = None

    async def create_service(**fields: Any) -> Any:
        fields.pop("created_by", None)
        return build_service_model(**fields)

    async def update_service(service: Any, *, updated_by: Any = None, **fields: Any) -> Any:
        for name, value in fields.items():
            setattr(service, name, value)
        return service

    mock.create_service.side_effect = create_service
    mock.update_service.side_effect = update_service
    return mock


class TestCreateService:
    async def test_creates_and_audits(self, repo: AsyncMock) -> None:
        service, session, audit = _make_service(repo)

        result = await service.create_service(
            HOSPITAL_ID, build_create_service_request(code="ecg", price="450.00"), actor_id=ACTOR_ID
        )

        assert result.code == "ECG"
        assert result.price == Decimal("450.00")
        assert repo.create_service.await_args.kwargs["hospital_id"] == HOSPITAL_ID
        assert session.commits == 1
        assert audit.actions() == ["service.created"]
        # Money goes into the audit record as an exact string, not a float.
        assert audit.last().changes["price"] == {"before": None, "after": "450.00"}

    async def test_a_duplicate_code_is_a_409_and_writes_nothing(self, repo: AsyncMock) -> None:
        repo.get_service_by_code.return_value = build_service_model()
        service, session, audit = _make_service(repo)

        with pytest.raises(DuplicateServiceCodeError) as excinfo:
            await service.create_service(HOSPITAL_ID, build_create_service_request())

        assert excinfo.value.status_code == 409
        repo.create_service.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_a_lost_race_on_the_constraint_is_a_409(self, repo: AsyncMock) -> None:
        repo.create_service.side_effect = IntegrityError(
            "INSERT", {}, Exception('violates unique constraint "uq_services_hospital_code"')
        )
        service, session, _ = _make_service(repo)

        with pytest.raises(DuplicateServiceCodeError):
            await service.create_service(HOSPITAL_ID, build_create_service_request())

        assert session.savepoints_rolled_back == 1
        assert session.commits == 0

    async def test_any_other_integrity_error_is_not_swallowed(self, repo: AsyncMock) -> None:
        repo.create_service.side_effect = IntegrityError(
            "INSERT", {}, Exception('violates check constraint "ck_services_price_non_negative"')
        )
        service, _, _ = _make_service(repo)

        with pytest.raises(IntegrityError):
            await service.create_service(HOSPITAL_ID, build_create_service_request())


class TestUpdateService:
    async def test_applies_only_what_changed(self, repo: AsyncMock) -> None:
        existing = build_service_model(hospital_id=HOSPITAL_ID, price=Decimal("500.00"))
        repo.get_service_by_id.return_value = existing
        service, session, audit = _make_service(repo)

        result = await service.update_service(
            HOSPITAL_ID,
            existing.id,
            # `taxable` is sent but already False, so it is not a change.
            build_update_service_request(price="650.00", taxable=False),
            actor_id=ACTOR_ID,
        )

        assert result.price == Decimal("650.00")
        assert set(repo.update_service.await_args.kwargs) == {"updated_by", "price"}
        assert session.commits == 1
        assert audit.actions() == ["service.updated"]
        assert audit.last().changes == {"price": {"before": "500.00", "after": "650.00"}}

    async def test_retiring_a_service(self, repo: AsyncMock) -> None:
        existing = build_service_model(hospital_id=HOSPITAL_ID)
        repo.get_service_by_id.return_value = existing
        service, _, audit = _make_service(repo)

        result = await service.update_service(
            HOSPITAL_ID, existing.id, build_update_service_request(is_active=False)
        )

        assert result.is_active is False
        assert audit.last().changes == {"is_active": {"before": True, "after": False}}

    async def test_a_no_op_update_writes_and_audits_nothing(self, repo: AsyncMock) -> None:
        existing = build_service_model(hospital_id=HOSPITAL_ID, price=Decimal("500.00"))
        repo.get_service_by_id.return_value = existing
        service, session, audit = _make_service(repo)

        result = await service.update_service(
            HOSPITAL_ID, existing.id, build_update_service_request(price="500.00")
        )

        assert result.id == existing.id
        repo.update_service.assert_not_awaited()
        assert session.commits == 0
        assert audit.events == []

    async def test_unknown_service_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_service_by_id.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(ServiceNotFoundError) as excinfo:
            await service.update_service(
                HOSPITAL_ID, uuid.uuid4(), build_update_service_request(price="1.00")
            )

        assert excinfo.value.status_code == 404


class TestQueries:
    async def test_get_service_is_tenant_scoped(self, repo: AsyncMock) -> None:
        existing = build_service_model(hospital_id=HOSPITAL_ID)
        repo.get_service_by_id.return_value = existing
        service, _, _ = _make_service(repo)

        result = await service.get_service(HOSPITAL_ID, existing.id)

        assert result.id == existing.id
        repo.get_service_by_id.assert_awaited_once_with(HOSPITAL_ID, existing.id)

    async def test_get_unknown_service_is_a_404(self, repo: AsyncMock) -> None:
        repo.get_service_by_id.return_value = None
        service, _, _ = _make_service(repo)

        with pytest.raises(ServiceNotFoundError):
            await service.get_service(HOSPITAL_ID, uuid.uuid4())

    async def test_list_passes_the_same_filters_to_list_and_count(self, repo: AsyncMock) -> None:
        repo.list_services.return_value = [build_service_model(), build_service_model()]
        repo.count_services.return_value = 9
        service, _, _ = _make_service(repo)

        page = await service.list_services(
            HOSPITAL_ID, term="cons", category="Consultation", is_active=True
        )

        assert len(page.items) == 2
        assert page.total_records == 9
        expected = {"term": "cons", "category": "Consultation", "is_active": True}
        assert repo.count_services.await_args.kwargs == expected
        assert repo.list_services.await_args.kwargs == {**expected, "skip": 0, "limit": 25}
