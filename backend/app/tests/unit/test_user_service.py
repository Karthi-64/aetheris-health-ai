"""Unit tests for :class:`UserService`.

Tests business logic with mocked repositories.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.core.exceptions import (
    BusinessRuleError,
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)
from app.core.security import hash_token
from app.models.user import User, UserStatus
from app.services.user_service import InvitationDelivery

#: Some moment in the past, for "this account has been used".
_EARLIER = datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


class _RecordingNotifier:
    """A notifier that keeps the requests it was given.

    ``queued`` is what :meth:`deliver_credential` answers: ``True`` unless a
    test is about an email that could not be queued. ``on_delivery`` is
    called at the moment a credential is handed over, for tests about what
    has and has not happened by then.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []
        self.queued: Any = True
        self.on_delivery: Any = None

    async def notify(self, request: Any) -> None:
        self.requests.append(request)

    async def deliver_credential(self, request: Any) -> Any:
        self.requests.append(request)
        if self.on_delivery is not None:
            self.on_delivery()
        return self.queued


#: Everything a notifier might answer that is not "an email was queued". Only
#: the value ``True`` itself counts: a mock, a truthy string or ``1`` must not.
_NOT_QUEUED = [False, None, 0, 1, "queued", MagicMock(name="not-a-bool")]


@pytest.fixture
def mock_user_repo() -> AsyncMock:
    """Create a mock UserRepository."""
    return AsyncMock()


@pytest.fixture
def mock_role_repo() -> AsyncMock:
    """Create a mock RoleRepository."""
    return AsyncMock()


@pytest.fixture
def mock_permission_repo() -> AsyncMock:
    """Create a mock PermissionRepository."""
    return AsyncMock()


@pytest.fixture
def mock_auth_service() -> AsyncMock:
    """Create a mock AuthService."""
    return AsyncMock()


@pytest.fixture
def mock_password_reset_repo() -> AsyncMock:
    """Create a mock PasswordResetTokenRepository."""
    return AsyncMock()


@pytest.fixture
def user_service(
    mock_user_repo: AsyncMock,
    mock_role_repo: AsyncMock,
    mock_permission_repo: AsyncMock,
    mock_auth_service: AsyncMock,
    mock_uow: AsyncMock,
    audit_sink: Any,
    mock_password_reset_repo: AsyncMock,
) -> Any:
    """Create a UserService with mocked dependencies."""
    from app.services.user_service import UserService

    return UserService(
        user_repo=mock_user_repo,
        role_repo=mock_role_repo,
        permission_repo=mock_permission_repo,
        auth_service=mock_auth_service,
        uow=mock_uow,
        audit=audit_sink,
        password_reset_repo=mock_password_reset_repo,
    )


def _make_user(overrides: dict[str, Any] | None = None) -> User:
    """Create a test user with sensible defaults."""
    user_id = uuid.uuid4()
    hospital_id = uuid.uuid4()

    user = MagicMock(spec=User)
    user.id = user_id
    user.hospital_id = hospital_id
    user.email = "test@hospital.test"
    user.first_name = "Test"
    user.last_name = "User"
    user.phone = "+911234567890"
    user.status = UserStatus.ACTIVE
    user.mfa_enabled = False
    user.user_roles = []
    user.created_at = MagicMock()
    user.updated_at = MagicMock()

    if overrides:
        for key, value in overrides.items():
            if hasattr(user, key):
                setattr(user, key, value)

    return user


@pytest.fixture
def notifier() -> _RecordingNotifier:
    """What the service asked to have sent."""
    return _RecordingNotifier()


@pytest.fixture
def mailing_service(
    mock_user_repo: AsyncMock,
    mock_role_repo: AsyncMock,
    mock_permission_repo: AsyncMock,
    mock_auth_service: AsyncMock,
    mock_uow: AsyncMock,
    audit_sink: Any,
    mock_password_reset_repo: AsyncMock,
    notifier: _RecordingNotifier,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """A UserService with a mail transport configured and a recording notifier."""
    from app.services.user_service import UserService

    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
    mock_auth_service.admit_invitation_resend.return_value = True
    return UserService(
        user_repo=mock_user_repo,
        role_repo=mock_role_repo,
        permission_repo=mock_permission_repo,
        auth_service=mock_auth_service,
        uow=mock_uow,
        audit=audit_sink,
        password_reset_repo=mock_password_reset_repo,
        notifier=notifier,
    )


def _invited_user() -> User:
    return _make_user({"status": UserStatus.INVITED})


def _emailed_token(notifier: _RecordingNotifier) -> str:
    """The raw token in the one email the service asked for."""
    [request] = notifier.requests
    url: str = request.secret_variables["action_url"]
    assert "?token=" not in url, "the token must ride in the fragment, not the query"
    marker = "/reset-password#token="
    assert marker in url
    return url.split(marker, 1)[1]


# ── Read Tests ─────────────────────────────────────────────────────────────


class TestGetUser:
    """Tests for retrieving users."""

    async def test_get_user_success(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """Existing user is returned."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        result = await user_service.get_user(user_id=user.id)
        assert result.id == user.id
        assert result.email == "test@hospital.test"

    async def test_get_user_not_found(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """Non-existent user raises NotFoundError."""
        mock_user_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.get_user(user_id=uuid.uuid4())

    async def test_get_user_cross_hospital_blocked(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """User from different hospital is not visible."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        other_hospital_id = uuid.uuid4()
        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.get_user(user_id=user.id, actor_hospital_id=other_hospital_id)


# ── Invite Tests ──────────────────────────────────────────────────────────


class TestInviteUser:
    """Tests for inviting users."""

    async def test_invite_user_success(self: Any, user_service: Any, mock_user_repo: Any) -> None:
        """User is created with invited status."""
        hospital_id = uuid.uuid4()
        mock_user_repo.email_is_taken.return_value = False
        mock_user_repo.create.return_value = _make_user(
            {
                "status": UserStatus.INVITED,
                "hospital_id": hospital_id,
            }
        )

        result, delivery = await user_service.invite_user(
            hospital_id=hospital_id,
            email="newuser@hospital.test",
            first_name="New",
            last_name="User",
            actor_permissions=["user.create"],
        )

        assert result.status == UserStatus.INVITED
        # What comes back is a delivery state, never the activation link.
        assert isinstance(delivery, InvitationDelivery)
        mock_user_repo.create.assert_called_once()

    async def test_invite_user_refreshes_roles_into_the_response(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Roles assigned during the invite must appear on the returned user.

        Regression: ``user_roles`` is a selectin-loaded collection that had
        already loaded (empty) before ``add_role`` inserted the rows, so the
        invite response reported ``roles: []`` for a user that did have roles.
        """
        from unittest.mock import MagicMock

        from app.models.role import Role
        from app.models.user import UserRole

        hospital_id = uuid.uuid4()
        role_id = uuid.uuid4()
        created = _make_user()
        created.status = UserStatus.INVITED
        mock_user_repo.email_is_taken.return_value = False
        mock_user_repo.create.return_value = created
        mock_user_repo.has_role.return_value = False

        def _loaded_user_role(user: Any) -> Any:
            role = MagicMock(spec=Role)
            role.id = role_id
            role.name = "Nurse"
            role.description = "Care coordination"
            role.is_system = True
            role.role_permissions = []
            ur = MagicMock(spec=UserRole)
            ur.role = role
            ur.role_id = role_id
            user.user_roles = [ur]
            return user

        mock_user_repo.refresh = AsyncMock(side_effect=_loaded_user_role)

        # The requested role must pass the B5 tenant check: a system role
        # (hospital_id NULL) is visible to every hospital.
        requested_role = MagicMock(spec=Role)
        requested_role.id = role_id
        requested_role.hospital_id = None
        requested_role.is_system = True
        mock_role_repo.get_by_id.return_value = requested_role

        result, _ = await user_service.invite_user(
            hospital_id=hospital_id,
            email="newuser@hospital.test",
            first_name="New",
            last_name="User",
            role_ids=[role_id],
            actor_permissions=["user.create"],
        )

        mock_user_repo.refresh.assert_awaited_once_with(result)
        assert [r.role.name for r in result.user_roles] == ["Nurse"]

    async def test_invite_user_duplicate_email(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Duplicate email raises ConflictError (HTTP 409 per the contract)."""
        hospital_id = uuid.uuid4()
        mock_user_repo.email_is_taken.return_value = True

        with pytest.raises(ConflictError, match="cannot be used for a new user"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="existing@hospital.test",
                first_name="Existing",
                last_name="User",
                actor_permissions=["user.create"],
            )

    async def test_invite_user_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Missing permission raises error."""
        # Ensure get_by_email returns None so it won't interfere
        mock_user_repo.email_is_taken.return_value = False

        with pytest.raises(PermissionDeniedError, match="do not have permission"):
            await user_service.invite_user(
                hospital_id=uuid.uuid4(),
                email="test@hospital.test",
                first_name="Test",
                last_name="User",
                actor_permissions=[],
            )

    async def test_invite_user_rejects_unknown_role_id(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B5: an unresolvable role id fails the whole invite (all-or-nothing)."""
        hospital_id = uuid.uuid4()
        mock_user_repo.email_is_taken.return_value = False
        mock_role_repo.get_by_id.return_value = None

        with pytest.raises(NotFoundError, match="roles were not found"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="newuser@hospital.test",
                first_name="New",
                last_name="User",
                role_ids=[uuid.uuid4()],
                actor_permissions=["user.create"],
            )

        # All-or-nothing: the user must not have been created.
        mock_user_repo.create.assert_not_called()

    async def test_invite_user_rejects_foreign_hospital_role(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B5/B1: a role from another hospital fails the invite too."""
        hospital_id = uuid.uuid4()
        foreign_role = MagicMock(
            id=uuid.uuid4(), name="Other Hospital Role", hospital_id=uuid.uuid4()
        )
        mock_user_repo.email_is_taken.return_value = False
        mock_role_repo.get_by_id.return_value = foreign_role

        with pytest.raises(NotFoundError, match="roles were not found"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="newuser@hospital.test",
                first_name="New",
                last_name="User",
                role_ids=[foreign_role.id],
                actor_permissions=["user.create"],
            )

        mock_user_repo.create.assert_not_called()


# ── Deactivate Tests ──────────────────────────────────────────────────────


class TestDeactivateUser:
    """Tests for deactivating users."""

    async def test_deactivate_other_user(
        self: Any, user_service: Any, mock_user_repo: Any, mock_auth_service: Any
    ) -> None:
        """Admin can deactivate another user."""
        user = _make_user()
        admin_id = uuid.uuid4()
        mock_user_repo.get_by_id.return_value = user

        # Mock update to modify the user in-place (simulating SQLAlchemy flush)
        async def _update_in_place(instance: Any, **kwargs: Any) -> Any:
            for key, value in kwargs.items():
                setattr(instance, key, value)
            return instance

        mock_user_repo.update.side_effect = _update_in_place

        result = await user_service.deactivate_user(
            user_id=user.id,
            actor_user_id=admin_id,
            actor_hospital_id=user.hospital_id,
            actor_permissions=["user.deactivate"],
        )

        assert result.status == UserStatus.SUSPENDED
        # Exactly this call: suspension also forgets every trusted browser,
        # so it must not pass ``forget_devices=False`` as a role change does.
        mock_auth_service.logout_all.assert_called_once_with(user.id)

    async def test_deactivate_self_raises_error(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """Deactivating yourself is not allowed."""
        user = _make_user()

        with pytest.raises(BusinessRuleError, match="cannot deactivate yourself"):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=user.id,
                actor_permissions=["user.deactivate"],
            )

    async def test_deactivate_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: deactivating a user from another hospital is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=uuid.uuid4(),  # different hospital
                actor_permissions=["user.deactivate"],
            )

    async def test_deactivate_without_permission_fails_closed(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: empty permission list is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=user.hospital_id,
                actor_permissions=[],
            )

        with pytest.raises(PermissionDeniedError):
            await user_service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=user.hospital_id,
                actor_permissions=None,
            )

    async def test_reactivate_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: reactivating a user from another hospital is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.reactivate_user(
                user_id=user.id,
                actor_hospital_id=uuid.uuid4(),
                actor_permissions=["user.deactivate"],
            )

    async def test_reactivate_without_permission_fails_closed(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: reactivate now requires the permission explicitly."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.reactivate_user(
                user_id=user.id,
                actor_hospital_id=user.hospital_id,
                actor_permissions=None,
            )


# ── Invitation delivery ────────────────────────────────────────────────────


class TestInvitationLink:
    """The activation link leaves the service by email, or not at all."""

    async def _invite(self, service: Any, mock_user_repo: Any) -> tuple[User, Any]:
        mock_user_repo.email_is_taken.return_value = False
        mock_user_repo.create.return_value = _invited_user()
        user, delivery = await service.invite_user(
            hospital_id=uuid.uuid4(),
            email="newuser@hospital.test",
            first_name="New",
            last_name="User",
            actor_permissions=["user.create"],
            actor_id=uuid.uuid4(),
        )
        return user, delivery

    async def test_the_link_goes_only_into_the_emails_secret_variables(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
    ) -> None:
        """The caller gets a state. The token is stored hashed and emailed raw."""
        user, delivery = await self._invite(mailing_service, mock_user_repo)

        assert delivery is InvitationDelivery.QUEUED
        token = _emailed_token(notifier)
        [request] = notifier.requests
        assert request.kind == "auth.user_invited"
        assert request.recipient_user_ids == (user.id,)
        # ``variables`` reach the in-app notification; the token must not be there.
        assert token not in repr(request.variables)
        assert request.link is None
        stored = mock_password_reset_repo.create.await_args.kwargs
        assert stored["user_id"] == user.id
        assert stored["token_hash"] == hash_token(token)
        assert stored["token_hash"] != token
        # Nothing audited mentions it.
        assert token not in repr(audit_sink.events)
        assert hash_token(token) not in repr(audit_sink.events)

    async def test_an_earlier_link_is_killed_only_once_its_replacement_is_queued(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
    ) -> None:
        """Attack: lose the email after the earlier link has already been killed.

        Killing first and minting second left a window in which the invited
        person had no working link. The order is: mint, hand the link to the
        email queue, and only then kill what came before, sparing the new one.
        """
        at_delivery: list[list[str]] = []
        notifier.on_delivery = lambda: at_delivery.append(
            [call[0] for call in mock_password_reset_repo.method_calls]
        )

        user, _ = await self._invite(mailing_service, mock_user_repo)

        # When the email was queued the new token existed and nothing was dead yet.
        assert at_delivery == [["create"]]
        calls = [call[0] for call in mock_password_reset_repo.method_calls]
        assert calls == ["create", "invalidate_all_for_user"]
        minted = mock_password_reset_repo.create.return_value
        mock_password_reset_repo.invalidate_all_for_user.assert_awaited_once_with(
            user.id, keep=minted.id
        )
        mock_password_reset_repo.mark_as_used.assert_not_called()

    @pytest.mark.parametrize("answer", _NOT_QUEUED, ids=repr)
    async def test_a_link_whose_email_was_not_queued_is_killed_and_reported_unavailable(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
        mock_uow: Any,
        answer: Any,
    ) -> None:
        """Attack: a token with no email carrying it stays alive, reported as sent.

        Anything but ``True`` from the notifier means no email was queued. The
        token just minted is killed, no earlier link is touched, and the
        caller is told ``UNAVAILABLE``.
        """
        notifier.queued = answer

        user, delivery = await self._invite(mailing_service, mock_user_repo)

        assert delivery is InvitationDelivery.UNAVAILABLE
        minted = mock_password_reset_repo.create.return_value
        mock_password_reset_repo.mark_as_used.assert_awaited_once_with(minted)
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        # The account is still created, audited and committed, exactly once.
        assert user.status == UserStatus.INVITED
        assert audit_sink.actions() == ["user.invited"]
        assert mock_uow.commit.await_count == 1
        # The dead token appears nowhere it could be read from.
        token = _emailed_token(notifier)
        assert token not in repr(audit_sink.events)
        assert hash_token(token) not in repr(audit_sink.events)

    async def test_a_service_with_no_notifier_wired_never_leaves_a_live_link(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The default notifier queues nothing, and says so: the link must die."""
        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")

        _, delivery = await self._invite(user_service, mock_user_repo)

        assert delivery is InvitationDelivery.UNAVAILABLE
        mock_password_reset_repo.mark_as_used.assert_awaited_once_with(
            mock_password_reset_repo.create.return_value
        )
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()

    async def test_a_mock_notifier_that_answers_with_a_mock_is_not_believed(
        self: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_permission_repo: Any,
        mock_auth_service: Any,
        mock_uow: Any,
        audit_sink: Any,
        mock_password_reset_repo: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """An ``AsyncMock`` notifier returns a mock, which is truthy and is not ``True``."""
        from app.services.user_service import UserService

        monkeypatch.setattr(settings, "SMTP_HOST", "smtp.hospital.example")
        sloppy = AsyncMock()
        service = UserService(
            user_repo=mock_user_repo,
            role_repo=mock_role_repo,
            permission_repo=mock_permission_repo,
            auth_service=mock_auth_service,
            uow=mock_uow,
            audit=audit_sink,
            password_reset_repo=mock_password_reset_repo,
            notifier=sloppy,
        )

        _, delivery = await self._invite(service, mock_user_repo)

        sloppy.deliver_credential.assert_awaited_once()
        # The link went through ``deliver_credential`` and nothing else.
        sloppy.notify.assert_not_called()
        assert delivery is InvitationDelivery.UNAVAILABLE
        mock_password_reset_repo.mark_as_used.assert_awaited_once()

    @pytest.mark.parametrize("smtp_host", [None, "", "   "])
    async def test_without_a_mail_transport_no_token_is_minted(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        mock_uow: Any,
        monkeypatch: pytest.MonkeyPatch,
        smtp_host: str | None,
    ) -> None:
        """A link with nowhere to go would have to travel some way it must not."""
        monkeypatch.setattr(settings, "SMTP_HOST", smtp_host)

        user, delivery = await self._invite(mailing_service, mock_user_repo)

        assert delivery is InvitationDelivery.UNAVAILABLE
        assert user.status == UserStatus.INVITED
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []
        # The account itself is still created and committed.
        assert mock_uow.commit.await_count == 1


class TestResendInvitation:
    """``UserService.resend_invitation``."""

    async def _resend(self, service: Any, user: User, **overrides: Any) -> Any:
        arguments: dict[str, Any] = {
            "user_id": user.id,
            "actor_id": uuid.uuid4(),
            "actor_hospital_id": user.hospital_id,
            "actor_permissions": ["user.create"],
        }
        arguments.update(overrides)
        return await service.resend_invitation(**arguments)

    async def test_a_resend_kills_the_old_link_emails_a_new_one_and_is_audited(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_auth_service: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
        mock_uow: Any,
    ) -> None:
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user
        mock_user_repo.lock_for_authentication.return_value = user

        delivery = await self._resend(mailing_service, user)

        assert delivery is InvitationDelivery.QUEUED
        mock_auth_service.admit_invitation_resend.assert_awaited_once_with(user.id)
        mock_user_repo.lock_for_authentication.assert_awaited_once_with(user.id)
        # Every earlier link dies, and the one just emailed is spared.
        minted = mock_password_reset_repo.create.return_value
        mock_password_reset_repo.invalidate_all_for_user.assert_awaited_once_with(
            user.id, keep=minted.id
        )
        mock_password_reset_repo.mark_as_used.assert_not_called()
        token = _emailed_token(notifier)
        assert mock_password_reset_repo.create.await_args.kwargs["token_hash"] == hash_token(token)
        assert audit_sink.actions() == ["user.invitation_resent"]
        assert token not in repr(audit_sink.events)
        assert mock_uow.commit.await_count == 1

    @pytest.mark.parametrize("permissions", [None, [], ["user.read", "user.update"]])
    async def test_it_fails_closed_without_user_create(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        permissions: list[str] | None,
    ) -> None:
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await self._resend(mailing_service, user, actor_permissions=permissions)

        # Refused before the account is even looked up.
        mock_user_repo.get_by_id.assert_not_called()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        assert notifier.requests == []

    async def test_another_hospitals_user_is_not_found_and_untouched(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_auth_service: Any,
        notifier: _RecordingNotifier,
    ) -> None:
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await self._resend(mailing_service, user, actor_hospital_id=uuid.uuid4())

        mock_auth_service.admit_invitation_resend.assert_not_called()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []

    @pytest.mark.parametrize("status", [UserStatus.ACTIVE, UserStatus.SUSPENDED])
    async def test_an_account_that_is_not_invited_is_a_conflict(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_auth_service: Any,
        notifier: _RecordingNotifier,
        status: UserStatus,
    ) -> None:
        """No activation link for an account that already has an owner."""
        user = _make_user({"status": status})
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(ConflictError):
            await self._resend(mailing_service, user)

        # ...and the refusal costs the account none of its resend allowance.
        mock_auth_service.admit_invitation_resend.assert_not_called()
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []

    async def test_over_the_allowance_nothing_is_killed_and_nothing_is_sent(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_auth_service: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
    ) -> None:
        """A refused resend must not invalidate the link the invitee already holds."""
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user
        mock_auth_service.admit_invitation_resend.return_value = False

        with pytest.raises(RateLimitError):
            await self._resend(mailing_service, user)

        mock_user_repo.lock_for_authentication.assert_not_called()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []
        assert audit_sink.events == []

    @pytest.mark.parametrize("locked_status", [UserStatus.ACTIVE, UserStatus.SUSPENDED, None])
    async def test_an_account_activated_while_we_waited_for_the_lock_gets_no_link(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
        mock_uow: Any,
        locked_status: UserStatus | None,
    ) -> None:
        """The race with activation: the status is read again under the row lock.

        The first read says INVITED. By the time the row is locked the
        invitation has been redeemed (or the account suspended, or deleted).
        Minting a link now would leave a live link into an account in use.
        """
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user
        mock_user_repo.lock_for_authentication.return_value = (
            None if locked_status is None else _make_user({"status": locked_status, "id": user.id})
        )

        with pytest.raises(ConflictError):
            await self._resend(mailing_service, user)

        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []
        assert audit_sink.events == []
        mock_uow.commit.assert_not_called()

    @pytest.mark.parametrize("answer", _NOT_QUEUED, ids=repr)
    async def test_a_resend_whose_email_was_not_queued_leaves_the_earlier_link_alone(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
        mock_uow: Any,
        answer: Any,
    ) -> None:
        """Attack: resend while the queue is down, and the invitee is locked out.

        The earlier link must keep working, because nothing has replaced it.
        The new token dies, nothing is audited as "resent", and the caller is
        told ``UNAVAILABLE``, never ``QUEUED``.
        """
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user
        mock_user_repo.lock_for_authentication.return_value = user
        notifier.queued = answer

        delivery = await self._resend(mailing_service, user)

        assert delivery is InvitationDelivery.UNAVAILABLE
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_password_reset_repo.mark_as_used.assert_awaited_once_with(
            mock_password_reset_repo.create.return_value
        )
        assert audit_sink.events == [], "an invitation that was not sent was audited as resent"
        # The dead token is committed as dead rather than left to a rollback.
        assert mock_uow.commit.await_count == 1

    async def test_without_a_mail_transport_it_reports_unavailable_and_mints_nothing(
        self: Any,
        mailing_service: Any,
        mock_user_repo: Any,
        mock_password_reset_repo: Any,
        mock_auth_service: Any,
        notifier: _RecordingNotifier,
        audit_sink: Any,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(settings, "SMTP_HOST", None)
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user

        delivery = await self._resend(mailing_service, user)

        assert delivery is InvitationDelivery.UNAVAILABLE
        mock_auth_service.admit_invitation_resend.assert_not_called()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_password_reset_repo.create.assert_not_called()
        assert notifier.requests == []
        # Nothing was sent, so there is nothing to record as "resent".
        assert audit_sink.events == []


class TestLifecycleAroundAnInvitation:
    """Suspending and reactivating an account that has an invitation out."""

    async def test_deactivating_kills_outstanding_links(
        self: Any, user_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """A suspended account must not keep a working way in."""
        user = _invited_user()
        mock_user_repo.get_by_id.return_value = user

        await user_service.deactivate_user(
            user_id=user.id,
            actor_user_id=uuid.uuid4(),
            actor_hospital_id=user.hospital_id,
            actor_permissions=["user.deactivate"],
        )

        mock_password_reset_repo.invalidate_all_for_user.assert_awaited_once_with(user.id)
        assert mock_user_repo.update.await_args.kwargs == {"status": UserStatus.SUSPENDED}

    @pytest.mark.parametrize("operation", ["deactivate", "reactivate"])
    async def test_revoking_the_sessions_comes_last_because_it_commits(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_auth_service: Any,
        mock_password_reset_repo: Any,
        audit_sink: Any,
        operation: str,
    ) -> None:
        """Attack: interrupt a suspension after its first commit.

        ``AuthService.logout_all`` commits. Whatever is done after it is in a
        second transaction, and a failure there leaves the first one standing:
        a suspended account with a live link and no audit entry. So the status
        change, the dead links and the audit entry all come before it.
        """
        done: list[str] = []
        mock_user_repo.get_by_id.return_value = _make_user({"status": UserStatus.SUSPENDED})
        mock_user_repo.update.side_effect = lambda *_, **__: done.append("status")
        mock_password_reset_repo.invalidate_all_for_user.side_effect = lambda *_, **__: done.append(
            "links"
        )
        mock_auth_service.logout_all.side_effect = lambda *_: done.append(
            f"sessions after {audit_sink.actions()}"
        )

        await self._change(user_service, operation, mock_user_repo.get_by_id.return_value)

        assert done == ["status", "links", f"sessions after ['user.{operation}d']"]

    @pytest.mark.parametrize("operation", ["deactivate", "reactivate"])
    async def test_if_the_links_cannot_be_killed_nothing_is_committed(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_auth_service: Any,
        mock_password_reset_repo: Any,
        audit_sink: Any,
        mock_uow: Any,
        operation: str,
    ) -> None:
        """A failure before the last step must reach no commit at all."""
        user = _make_user({"status": UserStatus.SUSPENDED})
        mock_user_repo.get_by_id.return_value = user
        mock_password_reset_repo.invalidate_all_for_user.side_effect = RuntimeError("gone")

        with pytest.raises(RuntimeError, match="gone"):
            await self._change(user_service, operation, user)

        mock_auth_service.logout_all.assert_not_called()
        mock_uow.commit.assert_not_called()
        assert audit_sink.events == []

    async def test_reactivating_kills_outstanding_links_itself(
        self: Any, user_service: Any, mock_user_repo: Any, mock_password_reset_repo: Any
    ) -> None:
        """Attack: a link that outlived the suspension revives with the account.

        Reactivation does not trust the suspension to have cleaned up: every
        link, with none spared, is invalidated again.
        """
        user = _make_user({"status": UserStatus.SUSPENDED})
        user.password_changed_at = None
        user.last_login_at = None
        mock_user_repo.get_by_id.return_value = user

        await self._change(user_service, "reactivate", user)

        mock_password_reset_repo.invalidate_all_for_user.assert_awaited_once_with(user.id)
        assert mock_user_repo.update.await_args.kwargs == {"status": UserStatus.INVITED}

    @staticmethod
    async def _change(service: Any, operation: str, user: User) -> Any:
        """Suspend or reactivate ``user`` as an administrator of their hospital."""
        if operation == "deactivate":
            return await service.deactivate_user(
                user_id=user.id,
                actor_user_id=uuid.uuid4(),
                actor_hospital_id=user.hospital_id,
                actor_permissions=["user.deactivate"],
            )
        return await service.reactivate_user(
            user_id=user.id,
            actor_id=uuid.uuid4(),
            actor_hospital_id=user.hospital_id,
            actor_permissions=["user.deactivate"],
        )

    @pytest.mark.parametrize(
        ("password_changed_at", "last_login_at", "expected"),
        [
            (None, None, UserStatus.INVITED),
            (_EARLIER, None, UserStatus.ACTIVE),
            (None, _EARLIER, UserStatus.ACTIVE),
            (_EARLIER, _EARLIER, UserStatus.ACTIVE),
        ],
    )
    async def test_reactivation_never_activates_an_account_nobody_has_claimed(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_auth_service: Any,
        mock_password_reset_repo: Any,
        password_changed_at: datetime | None,
        last_login_at: datetime | None,
        expected: UserStatus,
    ) -> None:
        """Suspend-then-reactivate is not a way to turn an invitation into an account."""
        user = _make_user({"status": UserStatus.SUSPENDED})
        user.password_changed_at = password_changed_at
        user.last_login_at = last_login_at
        mock_user_repo.get_by_id.return_value = user

        await user_service.reactivate_user(
            user_id=user.id,
            actor_id=uuid.uuid4(),
            actor_hospital_id=user.hospital_id,
            actor_permissions=["user.deactivate"],
        )

        assert mock_user_repo.update.await_args.kwargs == {"status": expected}
        # Exactly this call: every browser the account was recognised on is forgotten.
        mock_auth_service.logout_all.assert_awaited_once_with(user.id)
        # Reactivating sends nothing: a new link is a separate, deliberate act.
        mock_password_reset_repo.create.assert_not_called()

    @pytest.mark.parametrize("status", [UserStatus.INVITED, UserStatus.ACTIVE])
    async def test_reactivating_an_account_that_is_not_suspended_is_refused_and_touches_nothing(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_auth_service: Any,
        mock_password_reset_repo: Any,
        audit_sink: Any,
        mock_uow: Any,
        status: UserStatus,
    ) -> None:
        """Attack: "reactivate" a colleague to kill their invitation and sign them out.

        Reactivation invalidates every outstanding link and ends every
        session. On an account that is not suspended that is a denial of
        service with nothing to undo, so it is a conflict and changes nothing.
        """
        user = _make_user({"status": status})
        user.password_changed_at = None
        user.last_login_at = None
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(ConflictError):
            await self._change(user_service, "reactivate", user)

        mock_user_repo.update.assert_not_called()
        mock_password_reset_repo.invalidate_all_for_user.assert_not_called()
        mock_auth_service.logout_all.assert_not_called()
        mock_uow.commit.assert_not_called()
        assert audit_sink.events == []


# ── Role Management Tests ──────────────────────────────────────────────────


class TestRoleManagement:
    """Tests for role assignment and removal."""

    async def test_assign_role(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Role is assigned and sessions are revoked."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        # A system role (hospital_id=None) is assignable from any tenant (B1).
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=role_id, name="Doctor", hospital_id=None
        )
        mock_user_repo.has_role.return_value = False

        await user_service.assign_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        # BR-9: the join row records who granted the role.
        mock_user_repo.add_role.assert_called_once_with(user.id, role_id, assigned_by=None)
        # Sessions end so new ones carry the new claims; the account's
        # credentials are not in doubt, so its browsers stay recognised.
        mock_auth_service.logout_all.assert_called_once_with(user.id, forget_devices=False)

    async def test_assign_role_already_assigned(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Re-assigning same role is idempotent."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=role_id, name="Doctor", hospital_id=None
        )
        mock_user_repo.has_role.return_value = True  # Already assigned

        await user_service.assign_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.add_role.assert_not_called()

    async def test_remove_role(
        self: Any, user_service: Any, mock_user_repo: Any, mock_auth_service: Any
    ) -> None:
        """Role is removed and sessions are revoked."""
        user = _make_user()
        role_id = uuid.uuid4()

        mock_user_repo.get_by_id.return_value = user
        mock_user_repo.remove_role.return_value = True  # Successfully removed

        await user_service.remove_role(
            user_id=user.id,
            role_id=role_id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.remove_role.assert_called_once_with(user.id, role_id)
        mock_auth_service.logout_all.assert_called_once_with(user.id, forget_devices=False)

    async def test_assign_role_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: ``None`` actor_permissions is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=None,
            )

    async def test_remove_role_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: empty permission list is denied, not skipped."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(PermissionDeniedError):
            await user_service.remove_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=[],
            )

    async def test_assign_role_cross_tenant_user_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B1: assigning a role to another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=uuid.uuid4(), name="Doctor", hospital_id=None
        )

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=uuid.uuid4(),
            )

    async def test_assign_role_cross_tenant_role_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """B1: a role from another hospital cannot be assigned."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = MagicMock(
            id=uuid.uuid4(), name="Other Hospital Role", hospital_id=uuid.uuid4()
        )

        with pytest.raises(NotFoundError, match="Role not found."):
            await user_service.assign_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

    async def test_remove_role_cross_tenant_user_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: removing a role from another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.remove_role(
                user_id=user.id,
                role_id=uuid.uuid4(),
                actor_permissions=["role.assign"],
                actor_hospital_id=uuid.uuid4(),
            )

    async def test_list_user_roles_cross_tenant_is_a_404(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B1: listing roles of another hospital's user is not found."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        with pytest.raises(NotFoundError, match="User not found."):
            await user_service.list_user_roles(user_id=user.id, actor_hospital_id=uuid.uuid4())

    async def test_update_user_fails_closed_without_permission(
        self: Any, user_service: Any, mock_user_repo: Any
    ) -> None:
        """B2: update_user denies ``None``/empty actor_permissions."""
        user = _make_user()
        mock_user_repo.get_by_id.return_value = user

        permission_cases: list[list[str] | None] = [None, []]
        for permissions in permission_cases:
            with pytest.raises(PermissionDeniedError):
                await user_service.update_user(
                    user_id=user.id,
                    actor_hospital_id=user.hospital_id,
                    actor_permissions=permissions,
                    last_name="Nope",
                )


# ── Privilege Escalation & Lockout Tests ───────────────────────────────────


def _role_granting(*codes: str, name: str = "Lab Technician") -> MagicMock:
    """Build a role mock whose ``role_permissions`` expose the given codes."""
    role = MagicMock()
    role.id = uuid.uuid4()
    role.name = name
    role.hospital_id = None
    role.role_permissions = [MagicMock(permission=MagicMock(code=code)) for code in codes]
    return role


class TestPrivilegeEscalation:
    """BR-8 / FR-3 / AC-3: an actor may only grant permissions they hold."""

    async def test_assign_role_rejects_permissions_the_actor_lacks(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Assigning a role that outranks the actor is denied."""
        user = _make_user()
        role = _role_granting("lab.create", "lab.update")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError, match="permissions you do not hold"):
            await user_service.assign_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

        mock_user_repo.add_role.assert_not_called()

    async def test_assign_role_reports_exactly_what_is_missing(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """The 403 names the permissions that blocked it, for a usable error."""
        user = _make_user()
        role = _role_granting("lab.create", "lab.update", "patient.read")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError) as exc_info:
            await user_service.assign_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign", "patient.read"],
                actor_hospital_id=user.hospital_id,
            )

        assert exc_info.value.detail["missing_permissions"] == ["lab.create", "lab.update"]

    async def test_assign_role_allows_a_role_the_actor_fully_covers(
        self: Any,
        user_service: Any,
        mock_user_repo: Any,
        mock_role_repo: Any,
        mock_auth_service: Any,
    ) -> None:
        """Holding every permission the role grants is enough — and no more."""
        user = _make_user()
        role = _role_granting("patient.read", "patient.create", name="Receptionist")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.has_role.return_value = False

        await user_service.assign_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign", "patient.read", "patient.create"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.add_role.assert_called_once_with(user.id, role.id, assigned_by=None)
        mock_auth_service.logout_all.assert_called_once_with(user.id, forget_devices=False)

    async def test_self_assignment_cannot_escalate(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """The reported hole: an admin granting *themselves* a richer role."""
        admin = _make_user()
        role = _role_granting("pharmacy.dispense", name="Pharmacist")

        mock_user_repo.get_by_id.return_value = admin
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError):
            await user_service.assign_role(
                user_id=admin.id,  # self
                role_id=role.id,
                actor_permissions=["role.assign", "user.read"],
                actor_id=admin.id,
                actor_hospital_id=admin.hospital_id,
            )

    async def test_invite_cannot_escalate_through_initial_roles(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """Inviting into a privileged role is the same hole — the token is returned."""
        hospital_id = uuid.uuid4()
        role = _role_granting("pharmacy.dispense", name="Pharmacist")

        mock_user_repo.email_is_taken.return_value = False
        mock_role_repo.get_by_id.return_value = role
        mock_role_repo.get_with_permissions.return_value = role

        with pytest.raises(PermissionDeniedError, match="permissions you do not hold"):
            await user_service.invite_user(
                hospital_id=hospital_id,
                email="escalate@hospital.test",
                first_name="Priv",
                last_name="Escalator",
                role_ids=[role.id],
                actor_permissions=["user.create"],
            )

        mock_user_repo.create.assert_not_called()


class TestAdministratorLockout:
    """§14: the last remaining administrator cannot give up the admin role."""

    async def test_removing_last_administrator_role_is_blocked(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """No other active holder of ``role.assign`` → refuse."""
        user = _make_user()
        role = _role_granting("role.assign", "user.read", name="Hospital Admin")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.count_other_active_holders.return_value = 0

        with pytest.raises(BusinessRuleError, match="last administrator"):
            await user_service.remove_role(
                user_id=user.id,
                role_id=role.id,
                actor_permissions=["role.assign"],
                actor_hospital_id=user.hospital_id,
            )

        mock_user_repo.remove_role.assert_not_called()

    async def test_removal_allowed_while_another_administrator_remains(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """A second active admin makes the removal safe."""
        user = _make_user()
        role = _role_granting("role.assign", "user.read", name="Hospital Admin")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.count_other_active_holders.return_value = 1
        mock_user_repo.remove_role.return_value = True

        await user_service.remove_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.remove_role.assert_called_once_with(user.id, role.id)

    async def test_non_administrative_role_removal_is_unaffected(
        self: Any, user_service: Any, mock_user_repo: Any, mock_role_repo: Any
    ) -> None:
        """A clinical role carries no lockout risk, so the count is never taken."""
        user = _make_user()
        role = _role_granting("patient.read", name="Nurse")

        mock_user_repo.get_by_id.return_value = user
        mock_role_repo.get_with_permissions.return_value = role
        mock_user_repo.remove_role.return_value = True

        await user_service.remove_role(
            user_id=user.id,
            role_id=role.id,
            actor_permissions=["role.assign"],
            actor_hospital_id=user.hospital_id,
        )

        mock_user_repo.count_other_active_holders.assert_not_called()
        mock_user_repo.remove_role.assert_called_once_with(user.id, role.id)
