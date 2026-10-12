"""Purpose consent and the policy gate (``docs/modules/15-patient-app.md`` §8.2).

A consent is one explicit act: one account, one purpose, one hospital (or
none, for a platform policy), one exact policy version. It is recorded with
when and from where it was given, and it is never overwritten — a new policy
version asks again and adds a row.

Two things in this service are relied on by everything else:

* **The version check.** A request that names any version other than the
  current one is refused. Continuing is never consent, and neither is
  agreeing to a text that has since changed.
* **The gate.** :meth:`ConsentService.ensure_policies_accepted` is what makes
  a required platform policy required. No platform policy is required today
  (see :mod:`app.services.patient_app.policies`), so it passes; the day one is
  switched on, every caller of it starts refusing accounts that have not
  accepted it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from app.core.exceptions import ConflictError, ValidationError
from app.core.logging import get_logger
from app.models.patient_consent import ConsentPurpose
from app.schemas.patient_app.auth import PendingPolicy
from app.services.patient_app.common import ClientContext, patient_event
from app.services.patient_app.errors import ConsentRequiredError
from app.services.patient_app.policies import CURRENT_POLICIES, Policy

if TYPE_CHECKING:
    import uuid
    from collections.abc import Sequence

    from app.core.audit import AuditSink
    from app.database.unit_of_work import UnitOfWork
    from app.models.patient_consent import PatientConsentRecord
    from app.repositories.patient_account_link_repository import PatientAccountLinkRepository
    from app.repositories.patient_consent_repository import PatientConsentRepository

__all__ = ["ConsentService"]

logger = get_logger(__name__)

#: Why a link ended when its consent was withdrawn.
_LINK_ENDED_CONSENT_WITHDRAWN = "consent_withdrawn"


class ConsentService:
    """Records, withdraws and checks patient consents.

    :param consents: Consent data access.
    :param links: Record links — withdrawing the record-link consent ends one.
    :param uow: The request's unit of work.
    :param audit: Where consent changes are recorded.
    :param policies: The policies in force. The application always uses
        :data:`~app.services.patient_app.policies.CURRENT_POLICIES`.
    """

    def __init__(
        self,
        consents: PatientConsentRepository,
        links: PatientAccountLinkRepository,
        uow: UnitOfWork,
        audit: AuditSink,
        *,
        policies: Sequence[Policy] = CURRENT_POLICIES,
    ) -> None:
        self._consents = consents
        self._links = links
        self._uow = uow
        self._audit = audit
        self._policies = {policy.purpose: policy for policy in policies}

    # ── Policies ─────────────────────────────────────────────────────────────

    def policy(self, purpose: ConsentPurpose) -> Policy:
        """The current policy for a purpose.

        :raises ValidationError: If no such policy is in force.
        """
        policy = self._policies.get(purpose)
        if policy is None:
            raise ValidationError(message="Unknown policy.")
        return policy

    def require_current_version(self, purposes: Sequence[ConsentPurpose], version: str) -> None:
        """Refuse a consent that was given to anything but the current text.

        :param purposes: Every purpose the action needs consent for.
        :param version: The policy version the client says was shown.
        :raises ConflictError: If it is not the current version of each of them.
        """
        if any(self.policy(purpose).version != version for purpose in purposes):
            raise ConflictError(
                message="The policy has changed. Please review the current version and try again."
            )

    async def pending_policies(self, account_id: uuid.UUID) -> list[PendingPolicy]:
        """The required platform policies this account has not accepted at their current version.

        :param account_id: The account.
        :returns: What is still to accept, in policy order. Empty when nothing is.
        """
        pending: list[PendingPolicy] = []
        for policy in self._policies.values():
            if policy.scope != "platform" or not policy.required:
                continue
            accepted = await self._consents.get_active(
                None,
                account_id=account_id,
                purpose=policy.purpose.value,
                policy_version=policy.version,
            )
            if accepted is None:
                pending.append(PendingPolicy(purpose=policy.purpose.value, version=policy.version))
        return pending

    async def ensure_policies_accepted(self, account_id: uuid.UUID) -> None:
        """The policy gate: refuse an account that still has a required policy to accept.

        :param account_id: The account.
        :raises ConsentRequiredError: If any required platform policy is pending.
        """
        if await self.pending_policies(account_id):
            raise ConsentRequiredError

    # ── Reading ──────────────────────────────────────────────────────────────

    async def is_in_force(
        self, account_id: uuid.UUID, purpose: ConsentPurpose, *, hospital_id: uuid.UUID | None
    ) -> bool:
        """Whether the account's consent to a purpose, at its current version, is in force.

        :param account_id: The account.
        :param purpose: The purpose.
        :param hospital_id: The hospital, for a hospital purpose.
        """
        record = await self._consents.get_active(
            hospital_id,
            account_id=account_id,
            purpose=purpose.value,
            policy_version=self.policy(purpose).version,
        )
        return record is not None

    async def has_ever_consented(
        self, account_id: uuid.UUID, purpose: ConsentPurpose, *, hospital_id: uuid.UUID | None
    ) -> bool:
        """Whether the account ever consented to a purpose, at any version, withdrawn or not.

        :param account_id: The account.
        :param purpose: The purpose.
        :param hospital_id: The hospital, for a hospital purpose.
        """
        return await self._consents.has_ever(
            hospital_id, account_id=account_id, purpose=purpose.value
        )

    # ── Writing ──────────────────────────────────────────────────────────────

    async def record(
        self,
        account_id: uuid.UUID,
        purpose: ConsentPurpose,
        *,
        hospital_id: uuid.UUID | None,
        policy_version: str,
        client: ClientContext | None = None,
    ) -> PatientConsentRecord:
        """Record one consent as part of a larger action. Does **not** commit.

        For the services whose action *is* the consent's occasion (linking,
        registering): the consent commits or rolls back with that action.
        Recording a consent that is already in force is a no-op.

        :param account_id: Who consented.
        :param purpose: What to.
        :param hospital_id: The hospital, for a hospital purpose; ``None`` for
            a platform policy.
        :param policy_version: The version that was shown. Must be current.
        :param client: Where the act came from, kept as evidence.
        :returns: The consent in force.
        :raises ConflictError: If ``policy_version`` is not the current one.
        :raises ValidationError: If the purpose and the hospital do not go together.
        """
        policy = self.policy(purpose)
        self.require_current_version([purpose], policy_version)
        if (policy.scope == "hospital") != (hospital_id is not None):
            raise ValidationError(message="This consent does not apply here.")

        existing = await self._consents.get_active(
            hospital_id,
            account_id=account_id,
            purpose=purpose.value,
            policy_version=policy_version,
        )
        if existing is not None:
            return existing

        record = await self._consents.add(
            hospital_id,
            account_id=account_id,
            purpose=purpose.value,
            policy_version=policy_version,
            now=datetime.now(UTC),
            ip_address=client.ip_address if client else None,
            user_agent=client.user_agent if client else None,
        )
        await self._audit.record(
            patient_event(
                "patient.consent.granted",
                target_type="patient_consent",
                target_id=record.id,
                account_id=account_id,
                hospital_id=hospital_id,
                context={"purpose": purpose.value, "policy_version": policy_version},
                client=client,
            )
        )
        return record

    async def accept(
        self,
        account_id: uuid.UUID,
        purpose: ConsentPurpose,
        *,
        policy_version: str,
        hospital_id: uuid.UUID | None = None,
        client: ClientContext | None = None,
    ) -> PatientConsentRecord:
        """Accept a policy as an act of its own, and commit it.

        :param account_id: Who consented.
        :param purpose: What to.
        :param policy_version: The version that was shown. Must be current.
        :param hospital_id: The hospital, for a hospital purpose.
        :param client: Where the act came from.
        :returns: The consent in force.
        :raises ConflictError: If ``policy_version`` is not the current one.
        """
        try:
            # In a savepoint: two acceptances of one policy at the same moment
            # both find nothing in force and both insert, and the unique index
            # lets one through. The loser undoes its own insert only — and its
            # audit row with it, since nothing was granted twice — and answers
            # with the consent that is in force, exactly as a later repeat does.
            async with self._uow.session.begin_nested():
                record = await self.record(
                    account_id,
                    purpose,
                    hospital_id=hospital_id,
                    policy_version=policy_version,
                    client=client,
                )
        except IntegrityError:
            existing = await self._consents.get_active(
                hospital_id,
                account_id=account_id,
                purpose=purpose.value,
                policy_version=policy_version,
            )
            if existing is None:
                # Not the duplicate this is here for. Never report a consent
                # that is not on record.
                raise
            await self._uow.commit()
            return existing
        await self._uow.commit()
        return record

    async def withdraw(
        self,
        account_id: uuid.UUID,
        purpose: ConsentPurpose,
        *,
        hospital_id: uuid.UUID | None = None,
        client: ClientContext | None = None,
    ) -> int:
        """Withdraw the account's consent to a purpose, at every version, and commit.

        Withdrawing the record-link consent ends the account's link at that
        hospital: no record of that hospital is shown without it.

        :param account_id: Who is withdrawing.
        :param purpose: What from.
        :param hospital_id: The hospital, for a hospital purpose.
        :param client: Where the act came from.
        :returns: How many consents were ended. Zero if none was in force.
        """
        now = datetime.now(UTC)
        records = await self._consents.list_active(
            hospital_id, account_id=account_id, purpose=purpose.value
        )
        for record in records:
            await self._consents.withdraw(record, now=now)
            await self._audit.record(
                patient_event(
                    "patient.consent.withdrawn",
                    target_type="patient_consent",
                    target_id=record.id,
                    account_id=account_id,
                    hospital_id=hospital_id,
                    context={"purpose": purpose.value, "policy_version": record.policy_version},
                    client=client,
                )
            )

        if purpose is ConsentPurpose.HOSPITAL_RECORD_LINK and hospital_id is not None:
            link = await self._links.get_active_self(hospital_id, account_id)
            if link is not None:
                await self._links.end_link(link, now=now, reason=_LINK_ENDED_CONSENT_WITHDRAWN)
                await self._audit.record(
                    patient_event(
                        "patient.link.ended",
                        target_type="patient_account_link",
                        target_id=link.id,
                        account_id=account_id,
                        hospital_id=hospital_id,
                        context={"reason": _LINK_ENDED_CONSENT_WITHDRAWN},
                        client=client,
                    )
                )

        await self._uow.commit()
        logger.info(
            "patient_consent_withdrawn",
            account_id=str(account_id),
            purpose=purpose.value,
            count=len(records),
        )
        return len(records)
