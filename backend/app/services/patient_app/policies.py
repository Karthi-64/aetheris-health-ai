"""The policies a patient can consent to, and the version of each that is current.

The one place a policy version is written down. A consent is always recorded
against the exact version shown, and a request that names any other version is
refused — the patient is shown the current text and asked again.

**The texts do not exist yet.** Legal has not supplied them
(``docs/modules/15-patient-app.md`` U-2), so every version below is a draft
marker. For the two platform policies that also means they are *defined but
not required*: nothing is gated on them today, and ``pending_policies`` is
empty. Switching ``required`` on for a platform policy is all it takes to gate
every record endpoint on it; the mechanism is in
:class:`~app.services.patient_app.consent_service.ConsentService` and is
tested with a required policy injected.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

from app.models.patient_consent import ConsentPurpose

__all__ = ["CURRENT_POLICIES", "DRAFT_VERSION", "Policy"]

#: Version marker for every policy until legal supplies the real texts.
DRAFT_VERSION: Final = "2026-10-draft"


@dataclass(frozen=True, slots=True)
class Policy:
    """One policy and its current version.

    :param purpose: What the consent is for.
    :param version: The current version of its text.
    :param scope: ``platform`` — given once, for the whole app — or
        ``hospital`` — given per hospital.
    :param required: For a platform policy, whether the app may be used
        without it. A hospital policy is always required by the action it
        belongs to (linking, registering) and by nothing else.
    """

    purpose: ConsentPurpose
    version: str
    scope: Literal["platform", "hospital"]
    required: bool


CURRENT_POLICIES: Final[tuple[Policy, ...]] = (
    Policy(ConsentPurpose.TERMS_OF_SERVICE, DRAFT_VERSION, "platform", required=False),
    Policy(ConsentPurpose.PRIVACY_NOTICE, DRAFT_VERSION, "platform", required=False),
    Policy(ConsentPurpose.HOSPITAL_RECORD_LINK, DRAFT_VERSION, "hospital", required=True),
    Policy(ConsentPurpose.HOSPITAL_REGISTRATION, DRAFT_VERSION, "hospital", required=True),
)
