"""The catalog of notification kinds, and the templates they render from.

``docs/modules/11-notifications.md`` §4 rule 1 says every notification has a
kind; FR-4 that it is rendered from a template with variables. Template
*management* — editing them per hospital — is v2.1, so for the MVP the
templates live here, in code, where a change to wording is reviewed like any
other change.

A kind decides three things:

- **What it says** — a title and body for the notification centre, and
  optionally a subject and body for email.
- **Where it goes by default** — its default channels.
- **Whether the recipient may opt out** — a ``critical`` kind is always
  delivered on its default channels, whatever the user's preferences say
  (AC-4; §14 names auth and security).

**In-app text never contains a secret.** The centre is a persistent,
re-readable record; an invite or reset token belongs only in the email, which
is why email bodies are separate templates with their own variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template
from typing import TYPE_CHECKING

from app.models.notification import NotificationChannel

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "KINDS",
    "USER_CHANNELS",
    "NotificationKind",
    "get_kind",
    "render",
    "resolve_channels",
]

_IN_APP = NotificationChannel.IN_APP
_EMAIL = NotificationChannel.EMAIL

#: Channels a user can set a preference for today. SMS arrives in v2.1.
USER_CHANNELS: tuple[NotificationChannel, ...] = (_IN_APP, _EMAIL)


@dataclass(frozen=True, slots=True)
class NotificationKind:
    """One kind of notification.

    :param code: Stable identifier, stored on every notification.
    :param category: Grouping for the preferences page (§12).
    :param label: Human-readable name for the preferences page.
    :param title: Template for the in-app headline.
    :param body: Template for the in-app text. Must not reference a secret.
    :param link: Default in-app path to open, or ``None``.
    :param default_channels: Channels used when the user has no preference.
    :param critical: The user cannot switch the default channels off.
    :param email_subject: Template for the email subject, if it has an email.
    :param email_body: Template for the email body, if it has an email.
    """

    code: str
    category: str
    label: str
    title: str
    body: str
    link: str | None
    default_channels: frozenset[NotificationChannel]
    critical: bool = False
    email_subject: str | None = None
    email_body: str | None = None

    @property
    def supports_email(self) -> bool:
        """Whether this kind has an email form at all."""
        return self.email_subject is not None and self.email_body is not None


KINDS: dict[str, NotificationKind] = {
    kind.code: kind
    for kind in (
        NotificationKind(
            code="auth.user_invited",
            category="Account",
            label="Invitation to join",
            title="Welcome to ${hospital_name}",
            body="${actor_name} invited you to ${hospital_name}. Your account is ready to use.",
            link=None,
            default_channels=frozenset({_IN_APP, _EMAIL}),
            critical=True,
            email_subject="You have been invited to ${hospital_name}",
            email_body=(
                "Hello ${first_name},\n\n"
                "${actor_name} has invited you to join ${hospital_name} on Aetheris.\n\n"
                "Set your password to activate your account:\n"
                "${action_url}\n\n"
                "This link can be used once and expires in ${expires_in}.\n\n"
                "If you were not expecting this invitation, you can ignore this email."
            ),
        ),
        NotificationKind(
            code="auth.password_reset_requested",
            category="Account",
            label="Password reset requested",
            title="Password reset requested",
            body=(
                "A password reset was requested for your account. If this was not you, "
                "tell your administrator."
            ),
            link=None,
            default_channels=frozenset({_IN_APP, _EMAIL}),
            critical=True,
            email_subject="Reset your Aetheris password",
            email_body=(
                "Hello ${first_name},\n\n"
                "We received a request to reset the password for your Aetheris account.\n\n"
                "Choose a new password here:\n"
                "${action_url}\n\n"
                "This link can be used once and expires in ${expires_in}.\n\n"
                "If you did not ask for this, you can ignore this email — your password "
                "has not been changed."
            ),
        ),
        NotificationKind(
            code="billing.discount_approval_requested",
            category="Billing",
            label="Discount awaiting approval",
            title="Discount awaiting your approval",
            body=(
                "${actor_name} applied a discount of ${discount_amount} to an invoice for "
                "${patient_name}. It cannot be issued until an admin approves it."
            ),
            link="/billing",
            default_channels=frozenset({_IN_APP}),
            email_subject="A discount is awaiting your approval",
            email_body=(
                "Hello ${first_name},\n\n"
                "${actor_name} applied a discount of ${discount_amount} to an invoice for "
                "${patient_name}. The invoice cannot be issued until an admin approves it.\n\n"
                "Review it in Billing:\n"
                "${action_url}"
            ),
        ),
        # Lab kinds have no email form on purpose: a result is clinical
        # information about a named patient, and stays inside the application.
        NotificationKind(
            code="lab.results_released",
            category="Laboratory",
            label="Lab results released",
            title="Lab results ready for ${patient_name}",
            body="Results for ${patient_name} have been released: ${test_names}.",
            link="/laboratory",
            default_channels=frozenset({_IN_APP}),
        ),
        NotificationKind(
            code="lab.critical_result",
            category="Laboratory",
            label="Critical lab result",
            title="Critical result for ${patient_name}",
            body=(
                "${test_name} for ${patient_name} is ${result}, which is in the critical "
                "range. The result has not been released yet."
            ),
            link="/laboratory",
            default_channels=frozenset({_IN_APP}),
            critical=True,
        ),
        NotificationKind(
            code="lab.result_amended",
            category="Laboratory",
            label="Lab result corrected",
            title="A lab result for ${patient_name} was corrected",
            body=(
                "${test_name} for ${patient_name} was corrected from ${previous_result} to "
                "${result}. Reason: ${reason}"
            ),
            link="/laboratory",
            default_channels=frozenset({_IN_APP}),
            critical=True,
        ),
        NotificationKind(
            code="inventory.low_stock",
            category="Inventory",
            label="Item running low",
            title="${item_name} is running low",
            body=(
                "${item_name} is down to ${quantity} ${unit_of_measure} across the hospital, "
                "at or below its reorder point of ${reorder_point}."
            ),
            link="/inventory",
            default_channels=frozenset({_IN_APP}),
            email_subject="${item_name} is running low",
            email_body=(
                "Hello ${first_name},\n\n"
                "${item_name} is down to ${quantity} ${unit_of_measure} across the hospital, "
                "at or below its reorder point of ${reorder_point}.\n\n"
                "Review stock and raise a purchase order:\n"
                "${action_url}"
            ),
        ),
        NotificationKind(
            code="system.broadcast",
            category="Announcements",
            label="Announcements from your hospital",
            title="${title}",
            body="${body}",
            link=None,
            default_channels=frozenset({_IN_APP}),
            email_subject="${title}",
            email_body="Hello ${first_name},\n\n${body}",
        ),
    )
}


def get_kind(code: str) -> NotificationKind:
    """Look a kind up by code.

    :param code: The kind code.
    :returns: The kind.
    :raises KeyError: If no such kind exists. An unknown kind is a programming
        error in the emitting module, not user input.
    """
    return KINDS[code]


class _SafeVariables(dict[str, str]):
    """Template variables that render a visible placeholder for a missing name.

    §11: "interpolation fails safely with a placeholder". A notification with
    ``[patient_name]`` in it is a visible bug; one that raised would be a lost
    notification, and one that rendered ``${patient_name}`` would look like a
    template leaked to the user.
    """

    def __missing__(self, key: str) -> str:
        return f"[{key}]"


def render(template: str, variables: Mapping[str, str]) -> str:
    """Interpolate ``${name}`` placeholders in a template.

    :param template: The template text.
    :param variables: Values by name.
    :returns: The rendered text. A name with no value renders as ``[name]``.
    """
    return Template(template).safe_substitute(_SafeVariables(variables))


def resolve_channels(
    kind: NotificationKind, preferences: Mapping[str, object] | None
) -> frozenset[NotificationChannel]:
    """Decide which channels a notification of this kind uses for one user.

    Business rule 2: the kind's defaults, adjusted by the user's preferences
    for that kind. AC-4: a critical kind keeps its default channels whatever
    the preferences say — a preference can add a channel to it, never remove
    one. A channel the kind has no template for is never used.

    :param kind: The notification kind.
    :param preferences: The user's stored preferences, kind code to
        ``{channel: bool}``. Anything malformed is ignored.
    :returns: The channels to deliver on. May be empty for a non-critical kind
        the user has switched off entirely.
    """
    channels = set(kind.default_channels)

    override = (preferences or {}).get(kind.code)
    if isinstance(override, dict):
        for channel in USER_CHANNELS:
            wanted = override.get(channel.value)
            if wanted is True:
                channels.add(channel)
            elif wanted is False:
                channels.discard(channel)

    if kind.critical:
        channels |= kind.default_channels
    if not kind.supports_email:
        channels.discard(_EMAIL)
    return frozenset(channels)
