"""Per-hospital feature flags.

Flags live as flat ``feature.*`` keys in the ``hospitals.settings`` JSONB. This
module is the single definition of each key and of what "on" means, so the
service that gates a feature and the route that reports it cannot disagree.

A flag is on only when the stored value is exactly ``True``. A string
``"false"`` or the number ``1`` is not on: a truthiness test would switch a
gated feature on for a hospital whose row merely holds an unexpected value.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "AI_SLOT_RECOMMENDATION",
    "KNOWN_FLAGS",
    "PATIENT_APP_ENABLED",
    "flag_is_on",
    "with_default_flag",
]

#: AI slot suggestions in the appointment booking dialog.
AI_SLOT_RECOMMENDATION: Final = "feature.ai.slot_recommendation"

#: The Patient App may be used with this hospital: patients can link to, or
#: register, a record here (``docs/modules/15-patient-app.md`` §6). Off unless
#: set. It is read only by the patient services and is deliberately not in
#: :data:`KNOWN_FLAGS`: the staff capability read does not report it.
PATIENT_APP_ENABLED: Final = "feature.patient_app.enabled"

#: Every flag the capability read may report. Nothing else in ``settings`` is
#: ever returned by it.
KNOWN_FLAGS: Final[tuple[str, ...]] = (AI_SLOT_RECOMMENDATION,)


def flag_is_on(settings: Mapping[str, Any] | None, key: str) -> bool:
    """Tell whether a flag is switched on.

    :param settings: The hospital's settings object, or ``None``.
    :param key: The flag key.
    :returns: ``True`` only when the stored value is exactly ``True``.
    """
    return (settings or {}).get(key) is True


def with_default_flag(
    settings: Mapping[str, Any] | None, key: str, value: bool
) -> dict[str, Any] | None:
    """Default a flag without overriding an explicit choice.

    :param settings: The hospital's settings object, or ``None``.
    :param key: The flag key.
    :param value: The value to set when the key is absent.
    :returns: A new settings dict with ``key`` set, or ``None`` when the key is
        already present (an explicit value, including ``False``, wins).
    """
    current = dict(settings or {})
    if key in current:
        return None
    current[key] = value
    return current
