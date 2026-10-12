"""Repository tests for :class:`~app.repositories.role_repository.RoleRepository`.

Run against a real PostgreSQL (``docs/11-TESTING_STRATEGY.md`` §2.2) because
what is under test is the SQL: the system-role visibility filter, tenant
scoping, ordering, and eager-loaded permission rows. None of that is
observable through a mock.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any

import pytest

from app.core.tenancy import TenantScopeRequiredError, cross_tenant
from app.models.permission import Permission
from app.models.role import RolePermission
from app.repositories.role_repository import RoleRepository

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.role import Role as RoleModel

pytestmark = pytest.mark.database


@pytest.fixture
def repository(db_session: AsyncSession) -> RoleRepository:
    """A repository bound to the rolled-back test session."""
    return RoleRepository(db_session)


async def _create_role(
    repository: RoleRepository,
    *,
    hospital_id: uuid.UUID | None,
    name: str | None = None,
    is_system: bool = False,
    **overrides: Any,
) -> RoleModel:
    """Insert a role with sensible defaults, overridable per test."""
    values: dict[str, Any] = {
        "name": name or f"Role {uuid.uuid4().hex[:8]}",
        "is_system": is_system,
    }
    values.update(overrides)
    return await repository.create(hospital_id=hospital_id, **values)


async def _create_permission(db_session: AsyncSession, *, code: str) -> Permission:
    """Insert a global permission row."""
    permission = Permission(
        id=uuid.uuid4(),
        code=code,
        module=code.split(".")[0],
        description=code,
    )
    db_session.add(permission)
    await db_session.flush()
    return permission


class TestVisibility:
    """System roles vs hospital roles vs another tenant's roles."""

    async def test_list_includes_system_and_own_roles(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """A tenant sees system roles plus its own — never another's."""
        await _create_role(repository, hospital_id=None, name="Super Admin", is_system=True)
        await _create_role(repository, hospital_id=hospital_id, name="Receptionist")
        await _create_role(repository, hospital_id=other_hospital_id, name="Other Hospital Role")

        names = {r.name for r in await repository.list_by_hospital(hospital_id)}

        assert names == {"Super Admin", "Receptionist"}

    async def test_count_agrees_with_list(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """The count uses the identical visibility filter as the list."""
        await _create_role(repository, hospital_id=None, name="Super Admin", is_system=True)
        await _create_role(repository, hospital_id=hospital_id, name="Receptionist")
        await _create_role(repository, hospital_id=other_hospital_id, name="Other Hospital Role")

        rows = await repository.list_by_hospital(hospital_id)
        total = await repository.count_by_hospital(hospital_id)

        assert total == len(rows) == 2

    async def test_exclude_system_returns_only_own(
        self, repository: RoleRepository, hospital_id: uuid.UUID
    ) -> None:
        """``include_system=False`` drops the shared system roles."""
        await _create_role(repository, hospital_id=None, name="Super Admin", is_system=True)
        await _create_role(repository, hospital_id=hospital_id, name="Receptionist")

        names = {
            r.name for r in await repository.list_by_hospital(hospital_id, include_system=False)
        }

        assert names == {"Receptionist"}

    async def test_list_orders_by_name(
        self, repository: RoleRepository, hospital_id: uuid.UUID
    ) -> None:
        """Roles are ordered by name so pagination is stable."""
        for name in ("Zulu", "Alpha", "Mike"):
            await _create_role(repository, hospital_id=hospital_id, name=name)

        rows = await repository.list_by_hospital(hospital_id)

        assert [r.name for r in rows] == ["Alpha", "Mike", "Zulu"]

    async def test_list_paginates(self, repository: RoleRepository, hospital_id: uuid.UUID) -> None:
        """Offset and limit slice the ordered result."""
        for index in range(5):
            await _create_role(repository, hospital_id=hospital_id, name=f"Role {index}")

        first = await repository.list_by_hospital(hospital_id, skip=0, limit=2)
        second = await repository.list_by_hospital(hospital_id, skip=2, limit=2)

        assert [r.name for r in first] == ["Role 0", "Role 1"]
        assert [r.name for r in second] == ["Role 2", "Role 3"]


class TestGetWithPermissions:
    """The detail lookup with eager-loaded permission rows."""

    async def test_returns_role_with_permission_codes(
        self,
        repository: RoleRepository,
        db_session: AsyncSession,
        hospital_id: uuid.UUID,
    ) -> None:
        """The role's permission rows come back in the same query."""
        role = await _create_role(repository, hospital_id=hospital_id, name="Doctor")
        perm_a = await _create_permission(db_session, code="patient.read")
        perm_b = await _create_permission(db_session, code="appointment.book")

        db_session.add(RolePermission(id=uuid.uuid4(), role_id=role.id, permission_id=perm_a.id))
        db_session.add(RolePermission(id=uuid.uuid4(), role_id=role.id, permission_id=perm_b.id))
        await db_session.flush()
        # The role was created through this same session, so its (empty)
        # role_permissions collection is already loaded in the identity map,
        # and SQLAlchemy will not overwrite a loaded collection on a later
        # query. Expiring first makes the eager load observable — a request
        # gets a fresh session and never hits this. Read the id before
        # expiring: touching an expired attribute would refresh it here, and
        # that IO cannot happen outside the async context.
        role_id = role.id
        db_session.expire_all()

        found = await repository.get_with_permissions(role_id, hospital_id=hospital_id)

        assert found is not None
        codes = {rp.permission.code for rp in found.role_permissions}
        assert codes == {"patient.read", "appointment.book"}

    async def test_system_role_visible_from_any_tenant(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """A system role can be read by every tenant."""
        system = await _create_role(
            repository, hospital_id=None, name="Super Admin", is_system=True
        )

        assert (
            await repository.get_with_permissions(system.id, hospital_id=other_hospital_id)
            is not None
        )

    async def test_another_tenants_role_is_invisible(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """A hospital-scoped role is invisible from another tenant."""
        role = await _create_role(repository, hospital_id=hospital_id, name="Receptionist")

        assert await repository.get_with_permissions(role.id, hospital_id=other_hospital_id) is None

    async def test_get_without_a_tenant_is_refused(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
    ) -> None:
        """Omitting ``hospital_id`` is an error, not an unrestricted read.

        It used to skip the filter and return any hospital's role.
        """
        role = await _create_role(repository, hospital_id=hospital_id, name="Receptionist")

        with pytest.raises(TenantScopeRequiredError):
            await repository.get_with_permissions(role.id)

    async def test_an_explicit_cross_tenant_read_reaches_any_role(
        self,
        repository: RoleRepository,
        hospital_id: uuid.UUID,
        other_hospital_id: uuid.UUID,
    ) -> None:
        """The Super Admin path still works, but only when asked for by name."""
        role = await _create_role(repository, hospital_id=hospital_id, name="Receptionist")

        found = await repository.get_with_permissions(
            role.id, hospital_id=cross_tenant("platform-level administrator")
        )

        assert found is not None
        assert found.id == role.id

    async def test_missing_role_returns_none(
        self, repository: RoleRepository, hospital_id: uuid.UUID
    ) -> None:
        """An unknown role id resolves to ``None``, not an error."""
        assert await repository.get_with_permissions(uuid.uuid4(), hospital_id=hospital_id) is None
