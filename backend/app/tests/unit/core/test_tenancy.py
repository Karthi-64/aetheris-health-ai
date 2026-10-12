"""Unit tests for :mod:`app.core.tenancy` and the repository tenancy contract.

No database. The second half is a structural guard over every repository: a
method that could run without a tenant fails here until someone classifies it.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import pkgutil
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

import app.models  # noqa: F401 — registers every mapper
import app.repositories as repositories_package
from app.core.tenancy import (
    CrossTenant,
    TenantScope,
    TenantScopeRequiredError,
    bind_tenant_scope,
    cross_tenant,
    current_tenant_scope,
    is_tenant_scoped,
    scope_for_principal,
    tenant_column_nullable,
    tenant_or_platform,
    tenant_scope,
)
from app.database import Base
from app.repositories.base import BaseRepository
from app.repositories.report_repository import ReportRepository


class TestMarkers:
    def test_cross_tenant_needs_a_reason(self) -> None:
        with pytest.raises(ValueError, match="needs a reason"):
            cross_tenant("")
        with pytest.raises(ValueError, match="needs a reason"):
            cross_tenant("   ")

    def test_cross_tenant_carries_its_reason(self) -> None:
        marker = cross_tenant("login: tenant not known yet")
        assert isinstance(marker, CrossTenant)
        assert marker.reason == "login: tenant not known yet"

    def test_tenant_or_platform_keeps_a_hospital(self) -> None:
        hospital_id = uuid.uuid4()
        assert tenant_or_platform(hospital_id, reason="x") == hospital_id

    def test_tenant_or_platform_makes_the_platform_branch_explicit(self) -> None:
        scope = tenant_or_platform(None, reason="platform administrator")
        assert isinstance(scope, CrossTenant)
        assert scope.reason == "platform administrator"


class TestTenantScope:
    def test_a_hospital_scope_needs_a_hospital(self) -> None:
        with pytest.raises(TenantScopeRequiredError):
            TenantScope.hospital(None)  # type: ignore[arg-type]

    def test_a_system_scope_needs_a_reason(self) -> None:
        with pytest.raises(ValueError, match="needs a reason"):
            TenantScope.system("")

    def test_a_staff_principal_is_confined_to_its_hospital(self) -> None:
        hospital_id = uuid.uuid4()
        scope = scope_for_principal(hospital_id)
        assert scope.hospital_id == hospital_id
        assert not scope.is_system

    def test_a_platform_principal_gets_an_explicit_system_scope(self) -> None:
        scope = scope_for_principal(None)
        assert scope.is_system
        assert scope.reason

    def test_nothing_is_bound_by_default(self) -> None:
        assert current_tenant_scope() is None

    def test_a_scope_block_restores_the_previous_scope(self) -> None:
        outer, inner = uuid.uuid4(), uuid.uuid4()
        with tenant_scope(TenantScope.hospital(outer)):
            with tenant_scope(TenantScope.hospital(inner)):
                assert current_tenant_scope() == TenantScope.hospital(inner)
            assert current_tenant_scope() == TenantScope.hospital(outer)
        assert current_tenant_scope() is None

    def test_a_scope_block_is_restored_when_the_block_raises(self) -> None:
        with (
            pytest.raises(RuntimeError, match="boom"),
            tenant_scope(TenantScope.hospital(uuid.uuid4())),
        ):
            raise RuntimeError("boom")
        assert current_tenant_scope() is None

    async def test_a_bound_scope_does_not_outlive_its_task(self) -> None:
        """What a request dependency binds is gone when the request's task ends."""

        async def _request() -> None:
            bind_tenant_scope(TenantScope.hospital(uuid.uuid4()))
            assert current_tenant_scope() is not None

        await asyncio.create_task(_request())

        assert current_tenant_scope() is None


#: Tables added for the authentication throttle that carry no ``hospital_id``,
#: and why that is right. ``is_tenant_scoped`` is false for each, so the
#: session's tenant guard does not apply to them — which is only safe for the
#: reasons written here.
TENANTLESS_AUTH_MODELS: dict[str, str] = {
    "AuthThrottleBucket": (
        "Keyed by a SHA-256 and holding only counters: no tenant data. It must exist "
        "before any account, and so any hospital, is known."
    ),
    "TrustedDevice": (
        "Hangs off one user exactly as a refresh token does; reached only by a user id "
        "the service resolved server-side. The hospital is the user's."
    ),
}


class TestModelClassification:
    def test_tables_with_a_hospital_column_are_tenant_scoped(self) -> None:
        from app.models.patient import Patient
        from app.models.permission import Permission
        from app.models.refresh_token import RefreshToken

        assert is_tenant_scoped(Patient)
        assert not is_tenant_scoped(Permission)
        assert not is_tenant_scoped(RefreshToken)

    def test_only_roles_and_audit_logs_allow_a_row_with_no_hospital(self) -> None:
        nullable = {
            mapper.class_.__tablename__
            for mapper in Base.registry.mappers
            if is_tenant_scoped(mapper.class_) and tenant_column_nullable(mapper.class_)
        }
        # ``patient_consent_records`` joined the two with the Patient App: a
        # consent to a platform policy (terms of service, privacy notice)
        # belongs to no hospital, a consent to a hospital purpose belongs to
        # that one. A check constraint ties the column to the purpose
        # (``test_a_hospital_consent_can_never_be_stored_without_its_hospital``).
        assert nullable == {"roles", "audit_logs", "patient_consent_records"}

    def test_a_hospital_consent_can_never_be_stored_without_its_hospital(self) -> None:
        """Attack: file a hospital consent under no hospital, where every tenant can see it."""
        from sqlalchemy import CheckConstraint

        checks = {
            constraint.name: str(constraint.sqltext)
            for constraint in Base.metadata.tables["patient_consent_records"].constraints
            if isinstance(constraint, CheckConstraint)
        }

        scope = next(text for name, text in checks.items() if str(name).endswith("hospital_scope"))
        assert "hospital_id IS NOT NULL" in scope
        assert "hospital_record_link" in scope
        assert "hospital_registration" in scope

    @pytest.mark.parametrize("name", sorted(TENANTLESS_AUTH_MODELS))
    def test_the_authentication_throttle_tables_are_deliberately_tenant_less(
        self, name: str
    ) -> None:
        """A table with no hospital column is outside the tenant guard — on purpose, with a reason.

        These two were added with the throttle. If either grew a
        ``hospital_id`` it would become tenant data and every query on it
        would need a scope; if a *new* tenant-less auth table appears, it has
        to be argued for here.
        """
        import app.models.auth_throttle as auth_throttle_models

        model = getattr(auth_throttle_models, name)

        assert not is_tenant_scoped(model)
        assert "hospital_id" not in model.__table__.c
        assert TENANTLESS_AUTH_MODELS[name].strip()

    def test_no_other_model_hides_in_the_auth_throttle_module(self) -> None:
        import app.models.auth_throttle as auth_throttle_models

        mapped = {
            mapper.class_.__name__
            for mapper in Base.registry.mappers
            if mapper.class_.__module__ == auth_throttle_models.__name__
        }

        assert mapped == set(TENANTLESS_AUTH_MODELS)

    def test_a_throttle_bucket_holds_counters_and_nothing_that_identifies_anyone(self) -> None:
        """Attack: read the throttle table to learn who is being attacked, or from where.

        A bucket is tenant-less because it holds no tenant data: a hash, the
        kind, and counters. No email, address, user id or hospital — if one is
        ever added, this table is no longer exempt from tenancy.
        """
        from app.models.auth_throttle import AuthThrottleBucket

        columns = set(AuthThrottleBucket.__table__.c.keys())

        assert columns == {
            "id",
            "key_hash",
            "kind",
            "failures",
            "blocked_until",
            "last_charged_at",
            "drains_at",
            "expires_at",
        }
        assert not AuthThrottleBucket.__table__.foreign_keys

    def test_a_trusted_device_reaches_a_hospital_only_through_its_user(self) -> None:
        """Like a refresh token: owned by one user, gone when the user is gone."""
        from app.models.auth_throttle import TrustedDevice

        foreign_keys = list(TrustedDevice.__table__.foreign_keys)

        assert [fk.target_fullname for fk in foreign_keys] == ["users.id"]
        assert foreign_keys[0].ondelete == "CASCADE"
        assert TrustedDevice.__table__.c.user_id.nullable is False
        # Only the hash of the device token is stored, never the token.
        assert "token" not in TrustedDevice.__table__.c
        assert "token_hash" in TrustedDevice.__table__.c

    def test_a_trusted_device_holds_timestamps_and_nothing_about_the_browser_or_the_tenant(
        self,
    ) -> None:
        """Attack: read the device table to learn where staff sign in from, or for which hospital.

        The row says *that* a browser completed a sign-in (and, since the MFA
        budgets, when it last passed the second factor) — never an address, a
        user agent, a hospital or the second factor itself. A column added
        here has to be argued for: with tenant data in it the table is no
        longer exempt from tenancy.
        """
        from app.models.auth_throttle import TrustedDevice

        columns = TrustedDevice.__table__.c

        assert set(columns.keys()) == {
            "id",
            "user_id",
            "token_hash",
            "created_at",
            "last_used_at",
            "expires_at",
            "mfa_verified_at",
        }
        # "Never passed the second factor" is the default, and the only safe one.
        assert columns.mfa_verified_at.nullable is True
        assert columns.mfa_verified_at.default is None
        assert columns.mfa_verified_at.server_default is None

    async def test_forgetting_who_passed_the_second_factor_is_confined_to_one_account(
        self,
    ) -> None:
        """Attack: switch MFA off on your own account and strip everyone's devices of their budget."""
        from sqlalchemy.dialects import postgresql

        from app.repositories.auth_throttle_repository import TrustedDeviceRepository

        executed: list[Any] = []

        class _Session:
            async def execute(self, statement: Any) -> Any:
                executed.append(statement)
                return SimpleNamespace(rowcount=0)

        target = uuid.uuid4()
        await TrustedDeviceRepository(_Session()).forget_mfa_for_user(target)  # type: ignore[arg-type]

        [statement] = executed
        compiled = statement.compile(dialect=postgresql.dialect())  # type: ignore[no-untyped-call]
        sql = " ".join(str(compiled).split())
        assert sql.startswith("UPDATE trusted_devices SET mfa_verified_at=")
        assert "trusted_devices.user_id = " in sql.partition(" WHERE ")[2]
        assert target in compiled.params.values()
        assert None in compiled.params.values()

    def test_the_throttle_repositories_are_outside_the_tenant_guard_only_because_of_that(
        self,
    ) -> None:
        """They do not extend ``BaseRepository``; the models above are the whole reason."""
        from app.repositories.auth_throttle_repository import (
            AuthThrottleRepository,
            TrustedDeviceRepository,
        )

        assert not issubclass(AuthThrottleRepository, BaseRepository)
        assert not issubclass(TrustedDeviceRepository, BaseRepository)

    def test_every_trusted_device_query_is_keyed_by_a_user_or_a_loaded_row(self) -> None:
        """Attack: a device token trusted for one account recognised for another.

        Every public method takes the account (``user_id``), a row already
        loaded for an account (``device``), or only hashes (``live_hashes``,
        which returns hashes and never a row).
        """
        from app.repositories.auth_throttle_repository import TrustedDeviceRepository

        unkeyed = sorted(
            name
            for name, member in _public_methods(TrustedDeviceRepository)
            if not {"user_id", "device"} & set(inspect.signature(member).parameters)
        )

        assert unkeyed == ["live_hashes"]


# ── Structural guard over every repository ──────────────────────────────────

#: Public repository methods on tenant-scoped models that neither take a
#: hospital nor an already-loaded row. Each is a deliberate decision with its
#: reason. A new one fails ``test_every_tenant_repository_method_is_classified``
#: until it is added here — which is the point.
EXPLICIT_EXCEPTIONS: dict[str, str] = {
    "AppointmentRepository.find_no_show_candidates": (
        "Scheduled job. Run under TenantScope.system; each row is then handled "
        "under its own hospital."
    ),
    "NotificationRepository.claim_due_deliveries": (
        "Scheduled job. Run under TenantScope.system; each row is then handled "
        "under its own hospital."
    ),
    "NotificationRepository.get_preferences": (
        "notification_preferences is keyed by user and has no hospital column."
    ),
    "NotificationRepository.get_preferences_for_users": (
        "notification_preferences is keyed by user; the users were resolved inside a hospital."
    ),
    "NotificationRepository.save_preferences": (
        "notification_preferences is keyed by user and has no hospital column."
    ),
    "UserRepository.get_by_email_cross_tenant": (
        "Login and password reset: the hospital is unknown until the account is found."
    ),
    "UserRepository.list_with_mfa_secret_cross_tenant": (
        "MFA key rotation: a retired key can be dropped only when no row anywhere uses it."
    ),
    "UserRepository.claim_authentication_attempt": (
        "Password-reset requests only: an advisory lock keyed on the email text alone; "
        "reads and writes no row."
    ),
    "UserRepository.lock_for_authentication": (
        "Taken only after a credential has been verified (before a session is issued, and "
        "when an emailed token is redeemed): the id comes from a server-side record and the "
        "hospital is read from the row."
    ),
    "UserRepository.has_role": "user_roles has no hospital column; the service scopes both ends.",
    "UserRepository.add_role": "user_roles has no hospital column; the service scopes both ends.",
    "UserRepository.remove_role": (
        "user_roles has no hospital column; the service scopes both ends."
    ),
}

#: Parameters that carry the tenant into a repository method.
_TENANT_PARAMETERS = {"hospital_id", "scope"}


def _all_repositories() -> list[type[BaseRepository[Any]]]:
    for module in pkgutil.iter_modules(repositories_package.__path__):
        importlib.import_module(f"{repositories_package.__name__}.{module.name}")

    found: set[type[BaseRepository[Any]]] = set()

    def _walk(cls: type[BaseRepository[Any]]) -> None:
        for subclass in cls.__subclasses__():
            found.add(subclass)
            _walk(subclass)

    _walk(BaseRepository)
    return sorted(found, key=lambda cls: cls.__name__)


def _model_of(repository: type[BaseRepository[Any]]) -> type[Any]:
    """Return the model a repository manages, by building it on a null session."""
    instance = repository(None)  # type: ignore[call-arg, arg-type]
    return instance._model  # noqa: SLF001 — the guard needs the managed model


def _public_methods(cls: type[Any]) -> list[tuple[str, Any]]:
    return [
        (name, member)
        for name, member in vars(cls).items()
        if not name.startswith("_") and inspect.iscoroutinefunction(member)
    ]


def _unclassified(cls: type[Any], model_names: set[str]) -> list[str]:
    """List methods that take neither a tenant nor an already-loaded row."""
    missing: list[str] = []
    for name, member in _public_methods(cls):
        parameters = list(inspect.signature(member).parameters.values())[1:]
        if any(parameter.name in _TENANT_PARAMETERS for parameter in parameters):
            continue
        # A method whose first argument is an ORM instance acts on a row that a
        # tenant-scoped read already returned; the session guard still refuses
        # it if that row belongs to another hospital.
        if parameters and str(parameters[0].annotation) in model_names:
            continue
        missing.append(f"{cls.__name__}.{name}")
    return missing


class TestRepositoryContract:
    def test_every_repository_is_discovered(self) -> None:
        # 23 BaseRepository subclasses today; the guard below is only as good
        # as this discovery, so a silent drop to zero must fail loudly.
        assert len(_all_repositories()) >= 23

    def test_every_tenant_repository_method_is_classified(self) -> None:
        """No public method on a tenant-scoped repository can run without a tenant.

        It must take ``hospital_id`` (or ``scope``), or act on an already-loaded
        row, or be listed in ``EXPLICIT_EXCEPTIONS`` with a reason.
        """
        model_names = {mapper.class_.__name__ for mapper in Base.registry.mappers}
        unclassified: list[str] = []
        for repository in _all_repositories():
            if not is_tenant_scoped(_model_of(repository)):
                continue
            unclassified.extend(_unclassified(repository, model_names))

        assert sorted(unclassified) == sorted(EXPLICIT_EXCEPTIONS)

    def test_every_report_query_takes_a_hospital(self) -> None:
        """Reports aggregate across rows, so every public query must be scoped."""
        model_names = {mapper.class_.__name__ for mapper in Base.registry.mappers}
        assert _unclassified(ReportRepository, model_names) == []

    def test_the_account_lockout_is_gone_from_the_repository(self) -> None:
        """Attack: lock a colleague out by failing their password five times.

        The methods that counted failures on the user row and locked the
        account were removed with the lockout. If one comes back — or stays
        listed as an exception to tenancy — the lockout is coming back.
        """
        from app.repositories.user_repository import UserRepository

        removed = ("increment_failed_logins", "reset_failed_logins", "lock_account")

        for name in removed:
            assert not hasattr(UserRepository, name)
            assert f"UserRepository.{name}" not in EXPLICIT_EXCEPTIONS
        assert not any(
            "lock_account" in key or "failed_login" in key for key in EXPLICIT_EXCEPTIONS
        )

    def test_the_authentication_exceptions_are_exactly_the_methods_that_still_exist(self) -> None:
        from app.repositories.user_repository import UserRepository

        for name in ("claim_authentication_attempt", "lock_for_authentication"):
            assert f"UserRepository.{name}" in EXPLICIT_EXCEPTIONS
            assert inspect.iscoroutinefunction(getattr(UserRepository, name))

    def test_every_listed_exception_names_a_method_that_exists(self) -> None:
        """A stale entry is a standing permission for a method nobody has reviewed."""
        repositories = {cls.__name__: cls for cls in _all_repositories()}

        for qualified in EXPLICIT_EXCEPTIONS:
            owner, _, method = qualified.partition(".")
            assert owner in repositories, qualified
            assert method in vars(repositories[owner]), qualified

    def test_every_exception_has_a_reason(self) -> None:
        assert all(reason.strip() for reason in EXPLICIT_EXCEPTIONS.values())

    @pytest.mark.parametrize(
        "primitive", ["get_by_id", "get_by_ids", "list", "update_by_pk", "count", "exists"]
    )
    def test_base_primitives_accept_a_scope(self, primitive: str) -> None:
        parameters = inspect.signature(getattr(BaseRepository, primitive)).parameters
        assert "scope" in parameters
        assert parameters["scope"].default is None
