"""The patient's own account: ``GET /patient/me``.

Read-only. Returns the account (with the phone number masked), its record
links and the policies still to accept — and nothing from any record.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.schemas.patient_app.account import PatientMeResponse
from app.schemas.patient_app.auth import PatientAccountSummary
from app.services.patient_app.common import mask_phone

if TYPE_CHECKING:
    from app.models.patient_account import PatientAccount
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = ["PatientAccountService", "account_summary"]


def account_summary(account: PatientAccount) -> PatientAccountSummary:
    """The account as the app may show it: never the full phone number."""
    return PatientAccountSummary(
        id=account.id, phone_masked=mask_phone(account.phone), status=account.status
    )


class PatientAccountService:
    """Describes an authenticated patient account.

    :param authorization: Resolves the account's links and whether each is honoured.
    :param consent: Which policies are still to accept.
    """

    def __init__(self, authorization: PatientAuthorization, consent: ConsentService) -> None:
        self._authorization = authorization
        self._consent = consent

    async def get_me(self, account: PatientAccount) -> PatientMeResponse:
        """Describe the signed-in account.

        :param account: The authenticated, active account.
        :returns: The account, its links and its pending policies.
        """
        return PatientMeResponse(
            account=account_summary(account),
            links=await self._authorization.describe_links(account),
            pending_policies=await self._consent.pending_policies(account.id),
        )
