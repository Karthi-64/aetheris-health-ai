"""Unit tests for the notification kind catalog.

``docs/modules/11-notifications.md`` §16 asks for unit tests of "template
interpolation, preference resolution". Both are pure functions here.
"""

from __future__ import annotations

from string import Template
from typing import Any

import pytest

from app.models.notification import NotificationChannel
from app.services.notification_catalog import (
    KINDS,
    NotificationKind,
    get_kind,
    render,
    resolve_channels,
)

IN_APP = NotificationChannel.IN_APP
EMAIL = NotificationChannel.EMAIL

#: Variables that must never reach text the notification centre stores.
SECRET_VARIABLES = {"action_url", "token"}


def _kind(**overrides: Any) -> NotificationKind:
    """A non-critical kind that defaults to in-app only and has an email form."""
    values: dict[str, Any] = {
        "code": "test.kind",
        "category": "Test",
        "label": "Test kind",
        "title": "Title",
        "body": "Body",
        "link": None,
        "default_channels": frozenset({IN_APP}),
        "email_subject": "Subject",
        "email_body": "Email body",
    }
    values.update(overrides)
    return NotificationKind(**values)


def _placeholders(template: str) -> set[str]:
    """Names referenced by a ``string.Template``."""
    return set(Template(template).get_identifiers())


class TestRender:
    def test_interpolates_named_placeholders(self) -> None:
        assert render("Hello ${name}, welcome to ${place}.", {"name": "Asha", "place": "Demo"}) == (
            "Hello Asha, welcome to Demo."
        )

    def test_a_missing_variable_renders_a_visible_placeholder(self) -> None:
        # §11: "interpolation fails safely with a placeholder" — it does not
        # raise, and it does not leak the raw ${...} template syntax.
        assert render("Hello ${name}.", {}) == "Hello [name]."

    def test_unused_variables_are_ignored(self) -> None:
        assert render("Hello.", {"name": "Asha"}) == "Hello."

    def test_a_value_is_not_itself_interpolated(self) -> None:
        # A user-supplied value containing template syntax must be inert.
        assert render("Note: ${body}", {"body": "${secret}", "secret": "leak"}) == (
            "Note: ${secret}"
        )

    def test_a_stray_dollar_sign_does_not_raise(self) -> None:
        assert render("Costs $5 for ${item}", {"item": "tea"}) == "Costs $5 for tea"


class TestCatalog:
    def test_codes_match_their_keys_and_fit_the_column(self) -> None:
        for code, kind in KINDS.items():
            assert kind.code == code
            assert len(code) <= 50

    def test_get_kind_raises_for_an_unknown_code(self) -> None:
        with pytest.raises(KeyError):
            get_kind("no.such.kind")

    @pytest.mark.parametrize("kind", list(KINDS.values()), ids=lambda kind: kind.code)
    def test_in_app_text_never_references_a_secret(self, kind: NotificationKind) -> None:
        # The notification centre is a persistent, re-readable record. A token
        # belongs only in the email.
        used = _placeholders(kind.title) | _placeholders(kind.body)

        assert not used & SECRET_VARIABLES

    @pytest.mark.parametrize("kind", list(KINDS.values()), ids=lambda kind: kind.code)
    def test_a_kind_that_defaults_to_email_has_an_email_template(
        self, kind: NotificationKind
    ) -> None:
        if EMAIL in kind.default_channels:
            assert kind.supports_email

    def test_the_account_security_kinds_are_critical(self) -> None:
        # §14: "critical types (auth, security)".
        assert get_kind("auth.user_invited").critical
        assert get_kind("auth.password_reset_requested").critical
        assert not get_kind("system.broadcast").critical


class TestResolveChannels:
    """Business rule 2, AC-3 and AC-4."""

    def test_no_preferences_means_the_defaults(self) -> None:
        assert resolve_channels(_kind(), None) == {IN_APP}
        assert resolve_channels(_kind(), {}) == {IN_APP}

    def test_a_user_can_add_email(self) -> None:
        assert resolve_channels(_kind(), {"test.kind": {"email": True}}) == {IN_APP, EMAIL}

    def test_a_user_can_switch_a_channel_off(self) -> None:
        assert resolve_channels(_kind(), {"test.kind": {"in_app": False}}) == frozenset()

    def test_email_only_is_possible(self) -> None:
        prefs = {"test.kind": {"in_app": False, "email": True}}

        assert resolve_channels(_kind(), prefs) == {EMAIL}

    def test_preferences_for_another_kind_do_not_apply(self) -> None:
        assert resolve_channels(_kind(), {"other.kind": {"in_app": False}}) == {IN_APP}

    def test_ac4_a_critical_kind_keeps_its_default_channels(self) -> None:
        kind = _kind(critical=True, default_channels=frozenset({IN_APP, EMAIL}))
        prefs = {"test.kind": {"in_app": False, "email": False}}

        assert resolve_channels(kind, prefs) == {IN_APP, EMAIL}

    def test_a_critical_kind_can_still_gain_a_channel(self) -> None:
        kind = _kind(critical=True)

        assert resolve_channels(kind, {"test.kind": {"email": True}}) == {IN_APP, EMAIL}

    def test_email_is_never_used_for_a_kind_with_no_email_template(self) -> None:
        kind = _kind(email_subject=None, email_body=None)

        assert resolve_channels(kind, {"test.kind": {"email": True}}) == {IN_APP}

    @pytest.mark.parametrize(
        "prefs",
        [
            {"test.kind": "all"},
            {"test.kind": ["email"]},
            {"test.kind": {"email": "yes"}},
            {"test.kind": {"sms": True}},
            {"test.kind": None},
        ],
    )
    def test_malformed_preferences_are_ignored(self, prefs: dict[str, Any]) -> None:
        assert resolve_channels(_kind(), prefs) == {IN_APP}
