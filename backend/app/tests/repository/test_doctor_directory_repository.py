"""Repository tests for the doctor directory queries of :class:`DoctorRepository`.

Against a real PostgreSQL, because what is under test is the SQL: the
``hospital_id`` filter on every table the query touches, who counts as listed,
literal search, ordering and paging.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import pytest

from app.repositories.doctor_repository import DoctorRepository
from app.tests.patient_app_helpers import insert_department, insert_doctor

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.database


@pytest.fixture
def repository(db_session: AsyncSession) -> DoctorRepository:
    return DoctorRepository(db_session)


async def _ids(
    repository: DoctorRepository,
    hospital_id: uuid.UUID,
    *,
    search: str | None = None,
    department_id: uuid.UUID | None = None,
) -> list[uuid.UUID]:
    entries, total = await repository.list_directory(
        hospital_id, search=search, department_id=department_id, limit=50
    )
    assert total == len(entries)
    return [entry.id for entry in entries]


class TestHospitalFilter:
    async def test_every_directory_query_is_scoped_to_the_hospital_it_is_given(
        self,
        repository: DoctorRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        mine_dept = await insert_department(db_session, hospital_id, name="Mine")
        theirs_dept = await insert_department(db_session, other_hospital_id, name="Theirs")
        mine = await insert_doctor(db_session, hospital_id, department_id=mine_dept.id)
        theirs = await insert_doctor(db_session, other_hospital_id, department_id=theirs_dept.id)

        assert await _ids(repository, hospital_id) == [mine.id]
        assert await _ids(repository, other_hospital_id) == [theirs.id]
        assert await _ids(repository, hospital_id, department_id=theirs_dept.id) == []
        assert await repository.get_directory_entry(hospital_id, theirs.id) is None
        assert await repository.get_directory_entry(other_hospital_id, mine.id) is None
        assert await repository.get_directory_entry(hospital_id, mine.id) is not None
        assert [
            department.id
            for department in await repository.list_directory_departments(hospital_id, limit=50)
        ] == [mine_dept.id]
        assert await _ids(repository, uuid.uuid4()) == []

    async def test_a_department_row_of_another_hospital_is_never_joined(
        self,
        repository: DoctorRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """Bad data: a doctor pointing at another hospital's department shows none."""
        foreign = await insert_department(db_session, other_hospital_id, name="Foreign")
        doctor = await insert_doctor(db_session, hospital_id, department_id=foreign.id)

        entry = await repository.get_directory_entry(hospital_id, doctor.id)

        assert entry is not None
        assert (entry.department_id, entry.department_name) == (None, None)
        assert await repository.list_directory_departments(hospital_id, limit=50) == []
        assert await repository.list_directory_departments(other_hospital_id, limit=50) == []


class TestWhoIsListed:
    async def test_only_an_active_scheduled_doctor_with_an_active_user(
        self, repository: DoctorRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        shown = await insert_doctor(db_session, hospital_id)
        hidden = [
            await insert_doctor(db_session, hospital_id, deleted=True),
            await insert_doctor(db_session, hospital_id, available=False),
            await insert_doctor(db_session, hospital_id, user_status="suspended"),
            await insert_doctor(db_session, hospital_id, user_deleted=True),
        ]

        assert await _ids(repository, hospital_id) == [shown.id]
        for doctor in hidden:
            assert await repository.get_directory_entry(hospital_id, doctor.id) is None

    async def test_an_entry_carries_only_directory_columns(
        self, repository: DoctorRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        doctor = await insert_doctor(db_session, hospital_id, bio="Bio")

        entry = await repository.get_directory_entry(hospital_id, doctor.id)

        assert entry is not None
        assert set(entry.__slots__) == {
            "id",
            "first_name",
            "last_name",
            "specialization",
            "qualifications",
            "languages",
            "bio",
            "department_id",
            "department_name",
        }


class TestSearchOrderAndPaging:
    async def test_search_is_a_literal_substring_of_the_name_or_the_specialization(
        self, repository: DoctorRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        asha = await insert_doctor(db_session, hospital_id, first_name="Asha", last_name="Menon")
        odd = await insert_doctor(
            db_session, hospital_id, first_name="100%_", last_name="Odd\\", specialization="ENT"
        )

        assert await _ids(repository, hospital_id, search="sha men") == [asha.id]
        assert await _ids(repository, hospital_id, search="CARDIO") == [asha.id]
        assert await _ids(repository, hospital_id, search="ent") == [odd.id]
        assert await _ids(repository, hospital_id, search="%") == [odd.id]
        assert await _ids(repository, hospital_id, search="_") == [odd.id]
        assert await _ids(repository, hospital_id, search="\\") == [odd.id]
        assert await _ids(repository, hospital_id, search="LICCANARY") == []
        assert await _ids(repository, hospital_id, search="staff-secret") == []

    async def test_pages_are_in_name_order_and_the_total_is_the_whole_match(
        self, repository: DoctorRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        c = await insert_doctor(db_session, hospital_id, first_name="C", last_name="rao")
        a = await insert_doctor(db_session, hospital_id, first_name="a", last_name="Rao")
        z = await insert_doctor(db_session, hospital_id, first_name="Z", last_name="Abel")

        walked = []
        for skip in range(4):
            entries, total = await repository.list_directory(hospital_id, skip=skip, limit=1)
            assert total == 3
            walked += [entry.id for entry in entries]

        assert walked == [z.id, a.id, c.id]

    async def test_departments_are_sorted_bounded_and_have_a_listed_doctor(
        self, repository: DoctorRepository, db_session: AsyncSession, hospital_id: uuid.UUID
    ) -> None:
        b = await insert_department(db_session, hospital_id, name="beta")
        a = await insert_department(db_session, hospital_id, name="Alpha")
        await insert_department(db_session, hospital_id, name="Empty")
        gone = await insert_department(db_session, hospital_id, name="Gone", deleted=True)
        only_hidden = await insert_department(db_session, hospital_id, name="Hidden")
        for department in (a, b, gone):
            await insert_doctor(db_session, hospital_id, department_id=department.id)
        await insert_doctor(db_session, hospital_id, department_id=only_hidden.id, deleted=True)

        everything = await repository.list_directory_departments(hospital_id, limit=50)

        assert [department.id for department in everything] == [a.id, b.id]
        assert [
            department.id
            for department in await repository.list_directory_departments(hospital_id, limit=1)
        ] == [a.id]
