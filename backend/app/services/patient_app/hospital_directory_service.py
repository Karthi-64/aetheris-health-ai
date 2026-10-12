"""Hospital discovery: which hospitals a patient can find, and what they are told.

``docs/modules/15-patient-app.md`` §11 and §27.6. This is reference data, not
patient data — no record link is needed to read it and nothing is audited —
but it is still answered to a signed-in patient only, and only once the
required policies are accepted. That is checked before anything is looked up.

**Which hospitals.** Only those open to patients, and that question is never
answered here: every hospital comes from
:class:`~app.services.patient_app.hospital_gate.PatientHospitalGate`, which
also resolves the reference in a path. A hospital that is unknown, inactive,
switched off, or named by something that is not a reference at all is one
``404`` with one message.

**What is told.** The fields of
:class:`~app.schemas.patient_app.hospitals.PatientHospital`, built here one by
one. The stored address is read through an allow-list of keys, and a stored
logo URL is passed on only if it is one a browser may safely be handed.

**``linked``.** Whether the caller holds a link at the hospital that is
honoured right now — exactly what ``GET /patient/me`` reports, from the same
call. The account is the one the token proved; no request names an account or
a record.
"""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING, Final
from urllib.parse import urlsplit

from app.core.config import settings
from app.core.exceptions import NotFoundError
from app.schemas.common import Page
from app.schemas.patient_app.hospitals import (
    FILTER_MAX_LENGTH,
    HospitalListing,
    PatientHospital,
    PatientHospitalAddress,
    PatientHospitalCities,
)

if TYPE_CHECKING:
    import uuid

    from app.models.patient_account import PatientAccount
    from app.repositories.hospital_repository import HospitalDirectoryEntry
    from app.services.patient_app.consent_service import ConsentService
    from app.services.patient_app.hospital_gate import PatientHospitalGate
    from app.services.patient_app.patient_authorization import PatientAuthorization

__all__ = ["MAX_CITIES", "HospitalDirectoryService"]

#: The most cities the filter is ever offered.
MAX_CITIES: Final = 200

#: The one message for every hospital that cannot be shown. It is, on purpose,
#: what the framework itself answers when a path matches no route: a reference
#: with a slash in it never reaches this service, and must not be told apart
#: from one that does.
_NOT_FOUND: Final = HTTPStatus.NOT_FOUND.phrase

#: The longest a stored text is shown at.
_TEXT_MAX: Final = 200
#: The longest logo URL passed on.
_LOGO_URL_MAX: Final = 2048


class HospitalDirectoryService:
    """Lists and describes the hospitals open to patients.

    :param gate: Which hospitals are open to patients.
    :param authorization: Resolves the caller's own links, for ``linked``.
    :param consent: The policy gate.
    """

    def __init__(
        self,
        gate: PatientHospitalGate,
        authorization: PatientAuthorization,
        consent: ConsentService,
    ) -> None:
        self._gate = gate
        self._authorization = authorization
        self._consent = consent

    async def discover(
        self,
        account: PatientAccount,
        *,
        search: str | None,
        city: str | None,
        page: int,
        page_size: int,
    ) -> Page[PatientHospital]:
        """One page of the hospitals open to patients, in name order.

        :param account: The authenticated, active account.
        :param search: Text the hospital's name must contain. Blank is absent.
        :param city: The city the hospital must be in. Blank is absent.
        :param page: 1-based page number.
        :param page_size: Hospitals per page.
        :returns: The page — empty beyond the last one — and the total.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        await self._consent.ensure_policies_accepted(account.id)
        entries, total = await self._gate.list_open(
            search=_typed(search),
            city=_typed(city),
            skip=(page - 1) * page_size,
            limit=page_size,
        )
        linked = await self._linked_hospitals(account) if entries else frozenset()
        return Page[PatientHospital](
            items=[_view(entry, linked=entry.id in linked) for entry in entries],
            page=page,
            page_size=page_size,
            total_records=total,
        )

    async def get_hospital(self, account: PatientAccount, hospital_ref: str) -> PatientHospital:
        """Describe one hospital that is open to patients.

        :param account: The authenticated, active account.
        :param hospital_ref: The hospital's code or id, from the path.
        :returns: The hospital.
        :raises ConsentRequiredError: If a required policy is pending.
        :raises NotFoundError: If the hospital cannot be shown, whatever the reason.
        """
        await self._consent.ensure_policies_accepted(account.id)
        entry = await self._gate.describe(hospital_ref)
        if entry is None:
            raise NotFoundError(_NOT_FOUND)
        linked = await self._linked_hospitals(account)
        return _view(entry, linked=entry.id in linked)

    async def list_cities(self, account: PatientAccount) -> PatientHospitalCities:
        """The cities a patient can filter the directory by.

        :param account: The authenticated, active account.
        :returns: The cities that have a hospital open to patients. A name too
            long to be sent back as a filter is not offered as one.
        :raises ConsentRequiredError: If a required policy is pending.
        """
        await self._consent.ensure_policies_accepted(account.id)
        return PatientHospitalCities(
            cities=await self._gate.open_cities(max_length=FILTER_MAX_LENGTH, limit=MAX_CITIES)
        )

    async def _linked_hospitals(self, account: PatientAccount) -> frozenset[uuid.UUID]:
        """The hospitals where the account's own link is honoured right now.

        The links ``GET /patient/me`` shows, less the suspended ones: read by
        the verified account id, each checked against the phone-binding rule.
        """
        links = await self._authorization.describe_links(account)
        return frozenset(link.hospital_id for link in links if not link.suspended)


def _typed(value: str | None) -> str | None:
    """A filter as typed: trimmed, and absent when nothing is left."""
    return (value or "").strip() or None


def _view(entry: HospitalDirectoryEntry, *, linked: bool) -> PatientHospital:
    """A hospital as a patient may see it. Every field is set here, by name."""
    stored = entry.address if isinstance(entry.address, dict) else {}
    return PatientHospital(
        ref=entry.slug,
        name=entry.name,
        address=PatientHospitalAddress(
            **{field: _text(stored.get(field)) for field in PatientHospitalAddress.model_fields}
        ),
        phone=_text(entry.phone),
        logo_url=_logo_url(entry.logo_url),
        timezone=entry.timezone,
        linked=linked,
        # Nothing in the data model can promote a hospital.
        listing=HospitalListing.STANDARD,
    )


def _text(stored: object) -> str | None:
    """A stored value as text to show: trimmed and bounded, or nothing if it is not text."""
    if not isinstance(stored, str):
        return None
    return stored.strip()[:_TEXT_MAX].rstrip() or None


def _logo_url(stored: object) -> str | None:
    """A stored logo URL, if it is one a browser may be handed.

    That is an absolute ``https://`` URL with a host and no credentials in it
    — or ``http://`` in development, where a local object store has no
    certificate. Anything else (a relative path, ``javascript:``, ``data:``)
    is no logo at all, rather than a value for each client to sanitise.
    """
    if not isinstance(stored, str):
        return None
    url = stored.strip()
    schemes = ("https://", "http://") if settings.is_development else ("https://",)
    if not url.startswith(schemes) or len(url) > _LOGO_URL_MAX:
        return None
    if any(char.isspace() or not char.isprintable() for char in url):
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if not parts.hostname or "@" in parts.netloc:
        return None
    return url
