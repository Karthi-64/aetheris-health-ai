"""Patient App authentication routes — one-time codes and sessions.

See ``docs/modules/15-patient-app.md`` §27.3 for the contract.

**Where the credentials travel.**

* The access token is returned in the response body and held in memory by the
  app.
* The refresh token is **never** in a response body. It travels only in a
  cookie that is ``HttpOnly``, ``SameSite=Strict``, host-only, scoped to
  ``/api/v1/patient/auth`` and — with the ``__Secure-`` name prefix — sent
  only over HTTPS.
* The device cookie marks a browser that has completed a sign-in. It is not a
  credential: it only decides which throttle bucket a request for a code
  draws on. ``__Host-`` prefixed, so nothing but this host can write it.

Refresh and logout are the only endpoints authenticated by a cookie, and so
the only ones that need — and have — the cross-site request check
(:func:`~app.api.dependencies.patient.require_patient_csrf`).
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse

from app.api.dependencies.patient import (
    get_client_context,
    get_patient_account,
    get_patient_auth_service,
    require_patient_csrf,
)
from app.core.config import settings
from app.core.envelope import error_envelope, success_envelope
from app.core.error_codes import ErrorCode
from app.core.exceptions import AuthenticationError
from app.models.patient_account import PatientAccount
from app.schemas.patient_app.auth import (
    OtpRequest,
    OtpVerifyRequest,
    PatientRefreshResponse,
    PatientSessionResponse,
)
from app.services.patient_app.common import ClientContext
from app.services.patient_app.patient_auth_service import (
    DEVICE_TOKENS_PER_COOKIE,
    REFRESH_TOKEN_LIFETIME,
    PatientAuthService,
)

router = APIRouter(prefix="/auth", tags=["Patient App — Authentication"])

#: The only path the refresh cookie is ever sent to.
_REFRESH_COOKIE_PATH = "/api/v1/patient/auth"
_REFRESH_COOKIE_MAX_AGE = int(REFRESH_TOKEN_LIFETIME.total_seconds())
#: One refresh token: what ``secrets.token_urlsafe(48)`` produces.
_REFRESH_TOKEN = re.compile(r"^[A-Za-z0-9_-]{64}$")

#: One device token: what ``secrets.token_urlsafe(32)`` produces.
_DEVICE_TOKEN = re.compile(r"^[A-Za-z0-9_-]{32,64}$")
_DEVICE_COOKIE_MAX_AGE = 180 * 24 * 60 * 60
#: The most device tokens read from one request: a bound on the work one
#: request can ask for.
_DEVICE_TOKENS_READ = 8 * DEVICE_TOKENS_PER_COOKIE

#: Tokens must not be kept by any cache between the API and the app.
_NO_STORE = "no-store"


def _refresh_cookie_name() -> str:
    """The refresh cookie's name: ``__Secure-`` prefixed whenever it is sent as Secure.

    Not ``__Host-``: that prefix requires ``Path=/``, and this cookie is
    deliberately confined to the authentication path.
    """
    if settings.PATIENT_COOKIE_SECURE:
        return "__Secure-atheris-patient-refresh"
    return "atheris-patient-refresh"


def _device_cookie_name() -> str:
    """The device cookie's name: ``__Host-`` prefixed whenever it is sent as Secure."""
    if settings.PATIENT_COOKIE_SECURE:
        return "__Host-atheris-patient-device"
    return "atheris-patient-device"


def _cookie_values(request: Request, name: str) -> list[str]:
    """Every value the request carries for a cookie name, from the raw headers.

    A cookie parser keeps one value per name. Reading them all means a second
    cookie of the same name cannot hide, or stand in for, the real one.
    """
    values: list[str] = []
    for header in request.headers.getlist("cookie"):
        for pair in header.split(";"):
            key, _, value = pair.strip().partition("=")
            if key == name:
                values.append(value.strip().strip('"'))
    return values


def _get_refresh_token(request: Request) -> str | None:
    """Read the refresh token a browser presented, if it presented exactly one.

    Two different values under the cookie's name means something other than
    this host wrote one of them. Neither is used.
    """
    values = set(_cookie_values(request, _refresh_cookie_name()))
    if len(values) != 1:
        return None
    token = values.pop()
    return token if _REFRESH_TOKEN.fullmatch(token) else None


def _get_device_tokens(request: Request) -> tuple[str, ...]:
    """Read the device tokens a browser presented. Anything malformed is dropped."""
    tokens: list[str] = []
    for value in _cookie_values(request, _device_cookie_name()):
        for token in value.split("."):
            if _DEVICE_TOKEN.fullmatch(token) and token not in tokens:
                tokens.append(token)
    return tuple(tokens[:_DEVICE_TOKENS_READ])


def _set_refresh_cookie(response: Response, token: str) -> None:
    """Give the browser its refresh token. No ``Domain``: the cookie is host-only."""
    response.set_cookie(
        key=_refresh_cookie_name(),
        value=token,
        max_age=_REFRESH_COOKIE_MAX_AGE,
        path=_REFRESH_COOKIE_PATH,
        secure=settings.PATIENT_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _clear_refresh_cookie(response: Response) -> None:
    """Tell the browser to drop its refresh token."""
    response.delete_cookie(
        key=_refresh_cookie_name(),
        path=_REFRESH_COOKIE_PATH,
        secure=settings.PATIENT_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _set_device_cookie(response: Response, value: str | None) -> None:
    """Give the browser its device cookie, if there is one to give."""
    if not value:
        return
    response.set_cookie(
        key=_device_cookie_name(),
        value=value,
        max_age=_DEVICE_COOKIE_MAX_AGE,
        path="/",
        secure=settings.PATIENT_COOKIE_SECURE,
        httponly=True,
        samesite="strict",
    )


def _session_rejected(request: Request) -> JSONResponse:
    """The one answer to a refresh that is refused: 401, and the cookie is cleared."""
    response = JSONResponse(
        status_code=401,
        content=error_envelope(
            "Authentication required.",
            error_code=ErrorCode.AUTHENTICATION_REQUIRED,
            request_id=getattr(request.state, "request_id", None),
        ),
        headers={"Cache-Control": _NO_STORE},
    )
    _clear_refresh_cookie(response)
    return response


@router.post(
    "/otp/request",
    status_code=202,
    summary="Request a sign-in code",
    description=(
        "Send a one-time code by SMS. The answer is identical whether or not an account "
        "exists for the number."
    ),
    responses={
        202: {"description": "A code was sent."},
        422: {"description": "Not a number a code can be sent to."},
        429: {"description": "A limit was reached (OTP_THROTTLED)."},
        503: {"description": "Sign-in by SMS is unavailable."},
    },
)
async def request_otp(
    payload: OtpRequest,
    request: Request,
    client: ClientContext = Depends(get_client_context),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
) -> dict[str, Any]:
    """Send a sign-in code to a phone number."""
    result = await auth_service.request_otp(payload.phone, client, _get_device_tokens(request))
    return success_envelope("Code sent.", data=result.model_dump(mode="json"))


@router.post(
    "/otp/verify",
    summary="Verify a sign-in code",
    description="Verify a one-time code and start a session. Sets the refresh cookie.",
    responses={
        200: {"description": "Signed in."},
        401: {"description": "The code was not accepted (OTP_INVALID)."},
    },
)
async def verify_otp(
    payload: OtpVerifyRequest,
    request: Request,
    response: Response,
    client: ClientContext = Depends(get_client_context),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
) -> dict[str, Any]:
    """Verify a sign-in code and start a session."""
    session = await auth_service.verify_otp(
        payload.challenge_id, payload.code, client, _get_device_tokens(request)
    )
    _set_refresh_cookie(response, session.refresh_token)
    _set_device_cookie(response, session.device_cookie)
    response.headers["Cache-Control"] = _NO_STORE
    body = PatientSessionResponse(
        access_token=session.access_token,
        expires_in=session.expires_in,
        account=session.account,
        pending_policies=session.pending_policies,
    )
    return success_envelope("Signed in.", data=body.model_dump(mode="json"))


@router.post(
    "/refresh",
    summary="Rotate the session",
    description=(
        "Exchange the refresh cookie for a new access token and a new refresh cookie. "
        "Requires the X-Atheris-Patient header."
    ),
    responses={
        200: {"description": "Session rotated."},
        401: {"description": "No valid session. The cookie is cleared."},
    },
    dependencies=[Depends(require_patient_csrf)],
)
async def refresh(
    request: Request,
    response: Response,
    client: ClientContext = Depends(get_client_context),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
) -> Any:
    """Rotate the session named by the refresh cookie."""
    token = _get_refresh_token(request)
    if token is None:
        return _session_rejected(request)
    try:
        session = await auth_service.refresh(token, client)
    except AuthenticationError:
        return _session_rejected(request)

    _set_refresh_cookie(response, session.refresh_token)
    response.headers["Cache-Control"] = _NO_STORE
    body = PatientRefreshResponse(access_token=session.access_token, expires_in=session.expires_in)
    return success_envelope("Session refreshed.", data=body.model_dump(mode="json"))


@router.post(
    "/logout",
    status_code=204,
    summary="End this session",
    description="Revoke the session named by the refresh cookie and clear the cookie.",
    responses={204: {"description": "Signed out."}},
    dependencies=[Depends(require_patient_csrf)],
)
async def logout(
    request: Request,
    client: ClientContext = Depends(get_client_context),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
) -> Response:
    """End the session named by the refresh cookie."""
    await auth_service.logout(_get_refresh_token(request), client)
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response


@router.post(
    "/logout-all",
    status_code=204,
    summary="End every session",
    description="Revoke every session of the signed-in account.",
    responses={204: {"description": "Signed out everywhere."}},
)
async def logout_all(
    account: PatientAccount = Depends(get_patient_account),
    client: ClientContext = Depends(get_client_context),
    auth_service: PatientAuthService = Depends(get_patient_auth_service),
) -> Response:
    """End every session of the signed-in account."""
    await auth_service.logout_all(account, client)
    response = Response(status_code=204)
    _clear_refresh_cookie(response)
    return response
