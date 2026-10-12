"""Authentication middleware.

Extracts and validates the JWT access token from the ``Authorization`` header
and places the caller's *identity* on ``request.state`` for downstream
middleware.

**This is not the authorization boundary.** It resolves identity cheaply and
never rejects a request: an absent, malformed, or expired token simply leaves
``request.state.user_id`` as ``None``. Enforcement stays at the route level in
``app.api.dependencies.auth`` — ``get_current_user`` re-verifies the token,
loads the user, and checks account status against the database, and
``require_permission`` checks the permission code
(``docs/03-ARCHITECTURE.md`` §4.2).

Two consumers rely on the state this sets:

* :mod:`app.middleware.rate_limit` — to bill a request to a user and a hospital
  rather than to a shared client IP (``docs/06-API_STANDARDS.md`` §15).
* :mod:`app.middleware.logging` — to correlate a request with its actor.

**Two kinds of principal.** A staff token sets ``user_id`` (and
``hospital_id``). A Patient App token (``docs/modules/15-patient-app.md``
§5.6) sets ``patient_account_id`` and nothing else: a patient is never
represented as a staff user, and in particular never as a ``user_id`` with no
hospital — which is what a platform administrator looks like. The two
verifiers refuse each other's tokens, so at most one of the two is ever set.

Deliberately no database access. This runs on *every* request including
unauthenticated and public ones, so it decodes the signed token and stops
there; a per-request user lookup here would double the query cost of the API
and duplicate what ``get_current_user`` already does.
"""

from __future__ import annotations

import uuid

import jwt as pyjwt
import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.security import verify_access_token, verify_patient_access_token

logger = structlog.get_logger(__name__)

_BEARER_PREFIX = "Bearer "


class AuthMiddleware(BaseHTTPMiddleware):
    """Resolve the bearer token to an identity on ``request.state``.

    Sets ``user_id``, ``hospital_id``, ``patient_account_id`` and
    ``raw_token``. All are ``None`` when the request carries no usable access
    token; ``user_id`` and ``patient_account_id`` are never both set.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """Decode the access token, if one is present, onto the request state.

        :param request: The incoming HTTP request.
        :param call_next: The next middleware or route handler.
        :returns: The HTTP response.
        """
        # Defaults must exist even when no token is supplied, so downstream
        # consumers can read the attributes unconditionally.
        request.state.user_id = None
        request.state.hospital_id = None
        request.state.patient_account_id = None
        request.state.raw_token = None

        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith(_BEARER_PREFIX):
            token = auth_header.removeprefix(_BEARER_PREFIX).strip()
            if token:
                request.state.raw_token = token
                self._apply_claims(request, token)

        return await call_next(request)

    @staticmethod
    def _apply_claims(request: Request, token: str) -> None:
        """Verify *token* and copy its identity claims onto ``request.state``.

        Invalid tokens are swallowed: rejecting here would turn this middleware
        into a second, divergent authorization boundary, and would break the
        public endpoints (``/healthz``, ``/api/v1/auth/login``) that legitimately
        carry no valid token.

        :param request: The request whose state is being populated.
        :param token: The raw JWT from the Authorization header.
        """
        try:
            payload = verify_access_token(token)
        except pyjwt.InvalidTokenError:
            # Covers expired, malformed, and bad-signature tokens — PyJWT raises
            # subclasses of InvalidTokenError for all of them. A patient token
            # lands here too (it carries an audience this verifier does not
            # expect), and is then identified as what it is.
            AuthMiddleware._apply_patient_claims(request, token)
            return

        if payload.get("type") != "access":
            logger.debug("auth_token_wrong_type", path=request.url.path)
            return

        try:
            request.state.user_id = uuid.UUID(str(payload["sub"]))
        except (KeyError, ValueError):
            logger.debug("auth_token_bad_subject", path=request.url.path)
            return

        hospital_id = payload.get("hospital_id")
        if hospital_id is not None:
            try:
                request.state.hospital_id = uuid.UUID(str(hospital_id))
            except ValueError:
                logger.debug("auth_token_bad_hospital", path=request.url.path)

    @staticmethod
    def _apply_patient_claims(request: Request, token: str) -> None:
        """Identify a Patient App token, if that is what *token* is.

        Sets ``patient_account_id`` only. ``user_id`` and ``hospital_id`` are
        left ``None``: nothing downstream may take a patient for a staff user.

        :param request: The request whose state is being populated.
        :param token: A bearer token the staff verifier did not accept.
        """
        try:
            payload = verify_patient_access_token(token)
            request.state.patient_account_id = uuid.UUID(str(payload["sub"]))
        except (pyjwt.InvalidTokenError, KeyError, ValueError):
            logger.debug("auth_token_unusable", path=request.url.path)


__all__ = ["AuthMiddleware"]
