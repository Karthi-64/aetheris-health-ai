"""Unit tests for the patient-safe records and grants endpoints' services.

No database: the repositories are mocked and the models are stubs. The flows
that depend on real queries, joins and the tenant guard are in
``app/tests/api/test_patient_records_api.py`` and
``app/tests/api/test_patient_access_grants_api.py``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import ConflictError, NotFoundError
from app.core.tenancy import CrossTenant
from app.models.lab import LabOrderStatus, LabResultFlag
from app.models.patient_consent import GrantStatus
from app.models.pharmacy import PrescriptionStatus
from app.repositories.hospital_repository import HospitalDirectoryEntry
from app.schemas.patient_app.records import PatientLabItem, PatientLabResult, PatientPrescription
from app.services.patient_app.access_grant_service import AccessGrantService
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.patient_authorization import PatientContext
from app.services.patient_app.records_service import PatientRecordsService
from app.tests.conftest import RecordingAuditSink

NOW = datetime(2026, 10, 15, 9, 0, tzinfo=UTC)

HOSPITAL_ID = uuid.uuid4()
SECOND_HOSPITAL_ID = uuid.uuid4()
PATIENT_ID = uuid.uuid4()
ACCOUNT_ID = uuid.uuid4()
DOCTOR_ID = uuid.uuid4()


@dataclass(frozen=True, slots=True)
class _GateSummary:
    """The little a PatientHospitalGate.resolve returns to a caller."""

    id: uuid.UUID
    slug: str = "aster-city"
    name: str = "Aster City Hospital"


def _hospital(hospital_id: Any = HOSPITAL_ID, slug: str = "aster-city") -> HospitalDirectoryEntry:
    return HospitalDirectoryEntry(
        id=hospital_id,
        name="Aster City Hospital",
        slug=slug,
        settings={},
        address={},
        phone=None,
        logo_url=None,
        timezone="Asia/Kolkata",
    )


def _context(hospital_id: Any = HOSPITAL_ID) -> PatientContext:
    return PatientContext(account_id=ACCOUNT_ID, hospital_id=hospital_id, patient_id=PATIENT_ID)


def _account(account_id: Any = ACCOUNT_ID) -> Any:
    """A stub of the authenticated patient account."""
    return SimpleNamespace(id=account_id, is_active=True)


def _doctor() -> SimpleNamespace:
    return SimpleNamespace(
        id=DOCTOR_ID,
        user=SimpleNamespace(first_name="Aditi ", last_name=" Sharma"),
        specialization=" Cardiology ",
    )


def _prescription(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": HOSPITAL_ID,
        "patient_id": PATIENT_ID,
        "status": PrescriptionStatus.PARTIALLY_DISPENSED,
        "prescribed_at": NOW - timedelta(days=2),
        "doctor": _doctor(),
        "items": [
            SimpleNamespace(
                medicine_name="Paracetamol",
                dosage="1 tablet",
                frequency="twice daily",
                duration_days=5,
                instructions="after food",
                quantity=10,
                quantity_dispensed=4,
            )
        ],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _lab_item(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "test_name": "Haemoglobin",
        "result_value": "13.5",
        "result_unit": "g/dL",
        "result_flag": LabResultFlag.NORMAL,
        "reference_low": 13.0,
        "reference_high": 17.0,
        "released_at": NOW - timedelta(days=1),
        "notes": "technician note that must never be shown",
        "amendments": [SimpleNamespace(amended_at=NOW - timedelta(days=1, hours=2))],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _lab_order(**overrides: Any) -> SimpleNamespace:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "hospital_id": HOSPITAL_ID,
        "patient_id": PATIENT_ID,
        "status": LabOrderStatus.RELEASED,
        "released_at": NOW - timedelta(days=1),
        "doctor": _doctor(),
        "notes": "internal note that must never be shown",
        "items": [_lab_item()],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _records_service(
    *,
    prescriptions: AsyncMock | None = None,
    lab_orders: AsyncMock | None = None,
    contexts: list[PatientContext] | None = None,
    hospital: HospitalDirectoryEntry | None = None,
) -> tuple[PatientRecordsService, AsyncMock, AsyncMock, Any]:
    """A service over only-double dependencies, with its doubles handed back."""
    prescriptions = prescriptions or AsyncMock()
    lab_orders = lab_orders or AsyncMock()
    authorization = AsyncMock()
    authorization.own_contexts.return_value = contexts if contexts is not None else [_context()]
    gate = AsyncMock()
    gate.describe.return_value = hospital or _hospital()
    gate.resolve.return_value = _GateSummary(id=HOSPITAL_ID)
    authorization.resolve_context.return_value = _context()
    service = PatientRecordsService(
        cast("Any", authorization), cast("Any", gate), prescriptions, lab_orders
    )
    return service, prescriptions, lab_orders, gate


def _status_at(row: SimpleNamespace, now: datetime) -> Any:
    """The derived status, the same rule the model states on the row."""
    if row.revoked_at is not None:
        return GrantStatus.REVOKED
    return GrantStatus.EXPIRED if row.expires_at <= now else GrantStatus.ACTIVE


def _grant(authorized_view: SimpleNamespace) -> SimpleNamespace:
    """The fields :meth:`AccessGrantService._view` reads, as a stub row."""
    row = SimpleNamespace(
        id=authorized_view.id,
        hospital_id=authorized_view.hospital_id,
        grantee_type=authorized_view.grantee_type,
        grantee_id=authorized_view.grantee_id,
        grantee_hospital_id=authorized_view.grantee_hospital_id,
        purpose_note=authorized_view.purpose_note,
        categories=authorized_view.categories,
        records_from=authorized_view.records_from,
        records_to=authorized_view.records_to,
        granted_at=authorized_view.granted_at,
        expires_at=authorized_view.expires_at,
        revoked_at=authorized_view.revoked_at,
        revoked_by_account_id=None,
        revoke_reason=None,
    )
    row.status_at = lambda now: _status_at(row, now)
    return row


def _stub_grant_row(
    *, granted_at: datetime, hospital_id: Any = HOSPITAL_ID, revoked_at: datetime | None = None
) -> SimpleNamespace:
    return _grant(
        cast(
            "Any",
            SimpleNamespace(
                id=uuid.uuid4(),
                hospital_id=hospital_id,
                grantee_type="hospital",
                grantee_id=hospital_id,
                grantee_hospital_id=hospital_id,
                purpose_note="consultation",
                categories=["prescriptions"],
                records_from=None,
                records_to=None,
                granted_at=granted_at,
                expires_at=NOW + timedelta(days=29),
                revoked_at=revoked_at,
            ),
        )
    )


def _grant_service(
    grants: AsyncMock, *, contexts: list[PatientContext] | None = None
) -> AccessGrantService:
    authorization = AsyncMock()
    authorization.own_contexts.return_value = contexts if contexts is not None else [_context()]
    audit = RecordingAuditSink()
    uow = AsyncMock()
    return AccessGrantService(
        grants,
        authorization,
        AsyncMock(),
        AsyncMock(),
        uow=cast("Any", uow),
        audit=cast("Any", audit),
    )


class TestPrescriptionList:
    async def test_the_list_spans_only_the_callers_own_records(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescriptions.list_for_patient_records.return_value = ([], 0)

        page = await service.list_prescriptions(_account())

        prescriptions.list_for_patient_records.assert_awaited_once()
        args = prescriptions.list_for_patient_records.await_args.args
        assert args[0] == [(HOSPITAL_ID, PATIENT_ID)]
        assert isinstance(args[1], CrossTenant)
        assert page.items == []
        assert page.total_records == 0

    async def test_an_empty_link_list_reads_nothing_but_split_nothing(self) -> None:
        service, prescriptions, lab_orders, _ = _records_service(contexts=[])
        prescriptions.list_for_patient_records.return_value = ([], 0)
        lab_orders.list_for_patient_records.return_value = ([], 0)

        assert (await service.list_prescriptions(_account())).items == []
        assert (await service.list_lab_results(_account())).items == []
        records, scope = prescriptions.list_for_patient_records.await_args.args
        assert records == []
        assert isinstance(scope, CrossTenant)

    async def test_pagination_is_offset_math_and_the_filter_is_passed_through(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescriptions.list_for_patient_records.return_value = ([], 0)

        await service.list_prescriptions(
            _account(), status=PrescriptionStatus.DISPENSED, page=3, page_size=10
        )

        kwargs = prescriptions.list_for_patient_records.await_args.kwargs
        assert kwargs["statuses"] == [PrescriptionStatus.DISPENSED]
        assert kwargs["skip"] == 20
        assert kwargs["limit"] == 10

    async def test_without_a_filter_every_status_is_listed(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescriptions.list_for_patient_records.return_value = ([], 0)

        await service.list_prescriptions(_account())

        # ``None`` means no status filter in the repository, either.
        assert prescriptions.list_for_patient_records.await_args.kwargs["statuses"] is None


class TestPrescriptionDetail:
    async def test_one_own_prescription_is_the_patient_safe_view(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescription = _prescription()
        prescriptions.get_for_patient_records.return_value = prescription

        view = await service.get_prescription(_account(), "aster-city", str(prescription.id))

        assert isinstance(view, PatientPrescription)
        assert view.ref == str(prescription.id)
        assert view.status is PrescriptionStatus.PARTIALLY_DISPENSED
        assert view.hospital.ref == "aster-city"
        assert view.doctor.name == "Aditi Sharma"
        assert view.doctor.specialization == "Cardiology"
        assert view.timezone == "Asia/Kolkata"
        (item,) = view.items
        assert item.quantity_dispensed == 4
        # U-3: the doctor's notes are not decided, so they are never returned.
        assert set(view.model_dump()) == {
            "ref",
            "status",
            "prescribed_at",
            "timezone",
            "hospital",
            "doctor",
            "items",
        }

    async def test_a_single_read_is_confined_to_the_hospital_it_names(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescriptions.get_for_patient_records.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_prescription(_account(), HOSPITAL_ID.hex, str(uuid.uuid4()))

        args = prescriptions.get_for_patient_records.await_args.args
        assert args[0] == [(HOSPITAL_ID, PATIENT_ID)]
        assert args[1] == HOSPITAL_ID

    async def test_a_malformed_reference_is_one_not_found_before_any_query(self) -> None:
        service, prescriptions, _, _ = _records_service()

        with pytest.raises(NotFoundError):
            await service.get_prescription(_account(), "aster-city", "%20medicines")

        prescriptions.get_for_patient_records.assert_not_awaited()

    async def test_somebody_elses_prescription_is_the_same_not_found(self) -> None:
        service, prescriptions, _, _ = _records_service()
        prescriptions.get_for_patient_records.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_prescription(_account(), "aster-city", str(uuid.uuid4()))


class TestLabResultReads:
    async def test_the_list_uses_the_repositorys_released_only_default(self) -> None:
        service, _, lab_orders, _ = _records_service()
        lab_orders.list_for_patient_records.return_value = ([], 0)

        await service.list_lab_results(_account())

        list(lab_orders.list_for_patient_records.await_args.args)
        # No ``statuses`` override is sent, so the repository's own
        # released-only default stands — the caller has widened nothing.
        assert "statuses" not in lab_orders.list_for_patient_records.await_args.kwargs

    async def test_a_result_shows_the_current_value_with_when_it_was_amended(self) -> None:
        service, _, lab_orders, _ = _records_service()
        order = _lab_order()
        lab_orders.get_for_patient_records.return_value = order

        view = await service.get_lab_result(_account(), "aster-city", str(order.id))

        assert isinstance(view, PatientLabResult)
        assert view.status is LabOrderStatus.RELEASED
        (item,) = view.items
        assert item.value == "13.5"
        assert item.flag is LabResultFlag.NORMAL
        assert item.reference_low == 13.0
        assert item.amended_on == (NOW - timedelta(days=1, hours=2)).date()
        assert view.timezone == "Asia/Kolkata"

    async def test_the_single_order_read_is_released_only_no_matter_what(self) -> None:
        service, _, lab_orders, _ = _records_service()
        lab_orders.get_for_patient_records.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_lab_result(_account(), "aster-city", str(uuid.uuid4()))

        # No statuses override is sent, so the repository's own released-only
        # default stands — the caller has widened nothing.
        kwargs = lab_orders.get_for_patient_records.await_args.kwargs
        assert "statuses" not in kwargs or kwargs["statuses"] == (LabOrderStatus.RELEASED,)

    async def test_an_unreleased_order_never_leaves_the_repository(self) -> None:
        # Even if a repository bug someday returned one, the view fails closed.
        order = _lab_order(released_at=None)
        service, _, lab_orders, _ = _records_service()
        lab_orders.get_for_patient_records.return_value = order

        with pytest.raises(NotFoundError):
            await service.get_lab_result(_account(), "aster-city", str(order.id))

    async def test_the_result_dto_never_carries_internal_fields(self) -> None:
        item_fields = set(PatientLabItem.model_fields)
        assert not item_fields & {"notes", "sample_id", "released_by", "price", "amended_by"}
        assert set(PatientLabResult.model_fields).isdisjoint({"invoice_id", "patient_id"})


class TestDetailHospitalResolution:
    async def test_an_unknown_or_closed_hospital_is_one_not_found(self) -> None:
        service, prescriptions, _, gate = _records_service()
        gate.resolve.return_value = None

        with pytest.raises(NotFoundError):
            await service.get_prescription(_account(), "no-such", str(uuid.uuid4()))

        prescriptions.get_for_patient_records.assert_not_awaited()

    async def test_a_required_policy_wins_over_the_not_found(self) -> None:
        service, _, _, _ = _records_service()
        authorization = cast("Any", service._authorization)  # noqa: SLF001
        authorization.resolve_context.side_effect = ConsentRequiredError

        with pytest.raises(ConsentRequiredError):
            await service.get_prescription(_account(), "aster-city", str(uuid.uuid4()))


class TestGrantListing:
    async def test_grants_are_merged_newest_first_across_the_honoured_links(self) -> None:
        first = _stub_grant_row(granted_at=NOW - timedelta(days=1))
        second = _stub_grant_row(granted_at=NOW - timedelta(days=9), hospital_id=SECOND_HOSPITAL_ID)
        grants_repo = AsyncMock()

        async def _side_effect(hospital_id: Any, **kwargs: Any) -> list[Any]:
            return [first] if hospital_id == HOSPITAL_ID else [second]

        grants_repo.list_for_grantor.side_effect = _side_effect
        service = _grant_service(grants_repo, contexts=[_context(), _context(SECOND_HOSPITAL_ID)])

        page = await service.list_grants(_account())

        assert [item.id for item in page.items] == [first.id, second.id]
        assert page.total_records == 2

    async def test_a_status_filter_keeps_only_that_state(self) -> None:
        active_row = _stub_grant_row(granted_at=NOW - timedelta(days=1))
        revoked_row = _stub_grant_row(
            granted_at=NOW - timedelta(days=2), revoked_at=NOW - timedelta(hours=1)
        )
        grants_repo = AsyncMock()

        async def _two(hospital_id: Any, **kwargs: Any) -> list[Any]:
            return [active_row, revoked_row]

        grants_repo.list_for_grantor.side_effect = _two
        service = _grant_service(grants_repo)

        everything = await service.list_grants(_account())
        assert everything.total_records == 2

        revoked_only = await service.list_grants(_account(), status=GrantStatus.REVOKED)
        assert revoked_only.total_records == 1
        assert revoked_only.items[0].status is GrantStatus.REVOKED

    async def test_paging_slices_after_the_merge(self) -> None:
        grants_repo = AsyncMock()
        rows = [
            _stub_grant_row(granted_at=NOW - timedelta(days=index), hospital_id=SECOND_HOSPITAL_ID)
            for index in range(1, 4)
        ]

        async def _three(hospital_id: Any, **kwargs: Any) -> list[Any]:
            return rows

        grants_repo.list_for_grantor.side_effect = _three
        service = _grant_service(grants_repo, contexts=[_context()])

        page_one = await service.list_grants(_account(), page=1, page_size=2)
        assert len(page_one.items) == 2
        assert page_one.total_records == 3
        assert page_one.items[0].granted_at > page_one.items[1].granted_at

        page_two = await service.list_grants(_account(), page=2, page_size=2)
        assert len(page_two.items) == 1


class TestGrantRevocation:
    async def test_revoking_without_a_hospital_uses_the_grant_itself(self) -> None:
        row = _stub_grant_row(granted_at=NOW - timedelta(days=1))
        grants_repo = AsyncMock()
        grants_repo.get_grant_of_grantor.return_value = row

        async def _stamp(grant: Any, *, now: datetime, account_id: Any, reason: Any) -> None:
            # What the real repository does to the row.
            grant.revoked_at = now
            grant.revoked_by_account_id = account_id
            grant.revoke_reason = reason

        grants_repo.revoke.side_effect = _stamp
        service = _grant_service(grants_repo)

        view = await service.revoke(_account(), None, row.id, reason="not anymore")

        grants_repo.get_grant_of_grantor.assert_awaited_once_with(
            row.id, grantor_account_id=ACCOUNT_ID
        )
        assert view.status is GrantStatus.REVOKED
        grants_repo.revoke.assert_awaited_once()
        kwargs = grants_repo.revoke.await_args.kwargs
        assert kwargs["account_id"] == ACCOUNT_ID
        assert kwargs["reason"] == "not anymore"

    async def test_a_repeat_with_the_http_rule_is_a_conflict(self) -> None:
        row = _stub_grant_row(
            granted_at=NOW - timedelta(days=1), revoked_at=NOW - timedelta(minutes=1)
        )
        grants_repo = AsyncMock()
        grants_repo.get_grant_of_grantor.return_value = row
        service = _grant_service(grants_repo)

        with pytest.raises(ConflictError):
            await service.revoke(_account(), None, row.id, repeat_conflict=True)

    async def test_a_repeat_without_the_rule_still_changes_nothing(self) -> None:
        row = _stub_grant_row(
            granted_at=NOW - timedelta(days=1), revoked_at=NOW - timedelta(minutes=1)
        )
        grants_repo = AsyncMock()
        grants_repo.get_grant_of_grantor.return_value = row
        service = _grant_service(grants_repo)

        view = await service.revoke(_account(), None, row.id)

        assert view.status is GrantStatus.REVOKED
        grants_repo.revoke.assert_not_awaited()

    async def test_somebody_elses_grant_is_not_found(self) -> None:
        grants_repo = AsyncMock()
        grants_repo.get_grant_of_grantor.return_value = None
        service = _grant_service(grants_repo)

        with pytest.raises(NotFoundError):
            await service.revoke(_account(), None, uuid.uuid4())
