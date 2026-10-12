"""The one way an authenticated patient account becomes a patient at a hospital.

``docs/modules/15-patient-app.md`` §5.7 and §6. Staff authorization is role →
permission. Patient authorization is different in kind — **scope = self** —
and it is decided here, in one place, for every request:

1. the account has accepted the required policies;
2. the hospital is active and has the Patient App switched on;
3. the account holds an active ``self`` link at that hospital;
4. the link is honoured right now (the phone-binding rule, below);
5. the record-link consent for that hospital is in force.

The result is a :class:`PatientContext` — ``(account_id, hospital_id,
patient_id)`` — in which the hospital and the patient both come from the
server's own link row. A record endpoint asks for a context and uses nothing
else; it never reads a ``patient_id`` from a request and never implements
ownership or consent logic of its own.

**The phone-binding rule (§4.4).** A link is honoured only while the linked
record is active and its phone equals the account's verified phone. It is
evaluated on every resolution, from the record as it is now, and never stored
— so when a hospital corrects a phone number, or a number is recycled to a
stranger, the link stops being honoured at the next request without anyone
having to remember to end it.

Every failure before step 5 is the same ``404``: the answer does not say
whether the hospital exists, whether it holds a record for that phone, or
whether a link was ever made.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.exceptions import NotFoundError
from app.core.tenancy import TenantScope, cross_tenant, tenant_scope
from app.models.patient import PatientStatus
from app.models.patient_consent import ConsentPurpose
from app.schemas.patient_app.account import PatientLink
from app.services.patient_app.errors import ConsentRequiredError

if TYPE_CHECKING:
    import uuid
    from datetime import datetime

    from app.models.patient_account import PatientAccount
    from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
    from app.repositories.patient_account_repository import PatientAccountRepository
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.hospital_gate import PatientHospitalGate
    from app.services.patient_service import PatientService

__all__ = ["PatientAuthorization", "PatientContext"]

#: An account's own links are the only way to learn where it is linked.
_OWN_LINKS = cross_tenant("patient account: its own record links, by verified account id")

_NOT_FOUND = "Not found."


@dataclass(frozen=True, slots=True)
class PatientContext:
    """A patient acting on their own record at one hospital.

    Every field is server-resolved. It is a different type from a staff
    principal on purpose, and always names exactly one hospital.

    :param account_id: The authenticated patient account.
    :param hospital_id: The hospital, from the account's link.
    :param patient_id: The patient record, from the account's link.
    """

    account_id: uuid.UUID
    hospital_id: uuid.UUID
    patient_id: uuid.UUID


class PatientAuthorization:
    """Resolves and checks a patient's standing at a hospital.

    :param accounts: Patient accounts.
    :param links: Record links.
    :param gate: Which hospitals are open to patients.
    :param patients: The existing patient service; the record is read through
        it, inside the hospital's own tenant scope.
    :param consent: Consent state and the policy gate.
    """

    def __init__(
        self,
        accounts: PatientAccountRepository,
        links: PatientAccountLinkRepository,
        gate: PatientHospitalGate,
        patients: PatientService,
        consent: ConsentService,
    ) -> None:
        self._accounts = accounts
        self._links = links
        self._gate = gate
        self._patients = patients
        self._consent = consent

    async def resolve_context(
        self, account: PatientAccount, hospital_id: uuid.UUID
    ) -> PatientContext:
        """Turn an authenticated account into a patient context at a hospital.

        :param account: The authenticated, active account.
        :param hospital_id: The hospital the request concerns. It only selects
            which of the account's links is looked for; the context's hospital
            is the link's own.
        :returns: The context to call record services with.
        :raises ConsentRequiredError: If a required policy, or the record-link
            consent for this hospital, is not in force.
        :raises NotFoundError: For every other refusal, indistinguishably.
        """
        await self._consent.ensure_policies_accepted(account.id)

        hospital = await self._gate.get_enabled(hospital_id)
        if hospital is None:
            raise NotFoundError(_NOT_FOUND)
        link = await self._links.get_active_self(hospital.id, account.id)
        if link is None:
            raise NotFoundError(_NOT_FOUND)
        if not await self.record_is_bound(link.hospital_id, link.patient_id, account.phone):
            raise NotFoundError(_NOT_FOUND)
        if not await self._consent.is_in_force(
            account.id, ConsentPurpose.HOSPITAL_RECORD_LINK, hospital_id=link.hospital_id
        ):
            raise ConsentRequiredError

        return PatientContext(
            account_id=account.id, hospital_id=link.hospital_id, patient_id=link.patient_id
        )

    async def own_contexts(self, account: PatientAccount) -> list[PatientContext]:
        """Every hospital where the account may act on its own record right now.

        The account's active links, each held to exactly the test
        :meth:`resolve_context` applies — hospital open, record still bound to
        the verified phone, record-link consent in force. A link that fails it
        contributes nothing, silently: the caller sees no record there.

        :param account: The authenticated, active account.
        :returns: One context per hospital that passes, oldest link first.
        :raises ConsentRequiredError: If a required platform policy is pending.
        """
        await self._consent.ensure_policies_accepted(account.id)
        contexts: list[PatientContext] = []
        for link in await self._links.list_active_for_account(account.id, _OWN_LINKS):
            try:
                contexts.append(await self.resolve_context(account, link.hospital_id))
            except (NotFoundError, ConsentRequiredError):
                continue
        return contexts

    async def ensure_policies_accepted(self, account_id: uuid.UUID) -> None:
        """Refuse an account that has a required policy pending.

        :param account_id: The authenticated account.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        await self._consent.ensure_policies_accepted(account_id)

    async def describe_links(self, account: PatientAccount) -> list[PatientLink]:
        """The account's active links, each with whether it is honoured right now.

        A server-side loop: the links are read by account, and each record is
        then read inside its own hospital's scope. No query spans hospitals
        for a patient.

        :param account: The authenticated account.
        :returns: One entry per active link whose hospital still exists.
        """
        views: list[PatientLink] = []
        for link in await self._links.list_active_for_account(account.id, _OWN_LINKS):
            hospital = await self._gate.get_enabled(link.hospital_id)
            if hospital is None:
                # The hospital is closed to patients. The link is kept — it
                # comes back if the hospital does — but nothing about it is
                # shown while it cannot be used.
                continue
            bound = await self.record_is_bound(link.hospital_id, link.patient_id, account.phone)
            views.append(
                PatientLink(
                    hospital_id=hospital.id,
                    hospital_ref=self._gate.public_ref(hospital),
                    hospital_name=hospital.name,
                    linked_at=link.linked_at,
                    suspended=not bound,
                )
            )
        return views

    async def grantor_link_is_live(
        self,
        account_id: uuid.UUID,
        hospital_id: uuid.UUID,
        patient_id: uuid.UUID,
        *,
        granted_at: datetime,
    ) -> bool:
        """Whether an account still stands behind a record it once shared.

        For the access-grant gate: a grant is usable only while its grantor is
        an active account holding an active, honoured ``self`` link to that
        very record, at a hospital still open to patients — **and that link
        is the one the grant was given under**.

        A grant row names its grantor, not a link. So the link is identified
        by time: a grant can only have been given under a link that already
        existed, which means the live link must have been established no
        later than the grant. A link made afterwards is a new link — the old
        one ended (§8.3.2: ending a link makes every grant from it unusable)
        — and linking again never brings an old grant back to life.

        :param account_id: The account that gave the grant.
        :param hospital_id: The source hospital.
        :param patient_id: The record the grant shares.
        :param granted_at: When the grant was given.
        """
        account = await self._accounts.get_by_id(account_id)
        if account is None or not account.is_active:
            return False
        if await self._gate.get_enabled(hospital_id) is None:
            return False
        link = await self._links.get_active_self(hospital_id, account_id)
        if link is None or link.patient_id != patient_id:
            return False
        if link.linked_at > granted_at:
            return False
        return await self.record_is_bound(hospital_id, patient_id, account.phone)

    async def record_is_bound(
        self, hospital_id: uuid.UUID, patient_id: uuid.UUID, phone: str
    ) -> bool:
        """The phone-binding rule: is this record active and on this phone, right now?

        Read through the existing patient service, with the session confined
        to the record's own hospital for the duration.

        :param hospital_id: The hospital that owns the record.
        :param patient_id: The record.
        :param phone: The account's verified phone, in E.164 form.
        """
        with tenant_scope(TenantScope.hospital(hospital_id)):
            try:
                patient = await self._patients.get_patient_details(
                    hospital_id, patient_id, include_inactive=True
                )
            except NotFoundError:
                return False
        return patient.status is PatientStatus.ACTIVE and patient.phone == phone
