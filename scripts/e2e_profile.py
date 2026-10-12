"""End-to-end verification of the self-service profile page.

Drives the real running backend (uvicorn on :8000) over HTTP, exercising the
endpoints the profile screen calls — module spec ``02-user-management.md`` §12
("Own profile page"):

  1. Invite a fresh user as the admin, then activate them the way the
     invited person does: with the link in the email queued for their
     address. The API never returns that link, so this step reads the queued
     email from the backend's database (see ``E2E_DB_URL`` below).
  2. GET  /users/me            — the page's initial load.
  3. PATCH /users/me           — name and phone.
  4. PATCH /users/me           — a malformed phone is rejected (422).
  5. Assert the account cannot reach the admin directory (403), proving the
     profile page needs no ``user.*`` permission.
  6. POST /auth/password/change — wrong current password is rejected, the
     right one succeeds, and the new password actually logs in.
  7. POST /auth/mfa/enroll → /mfa/confirm is refused with a bad code.

Configuration (environment):

``AETHERIS_API_BASE``
    Base URL of the running backend. Default ``http://127.0.0.1:8000/api/v1``.

``E2E_DB_URL``
    Plain ``postgresql://user[:password]@host[:port]/dbname`` URL of the
    database that backend is using. Required for step 1. It is used for one
    read-only query: the body of the invitation email queued for the address
    this run just invited. The script refuses to connect when it is unset,
    when it is not a plain ``postgresql://`` URL, or when the database is
    named ``aetheris`` or ``aetheris_test``: point the backend and this
    variable at a disposable end-to-end database.

The backend must have a mail transport configured (``SMTP_HOST``), otherwise
no link is ever created and the invite reports ``delivery: unavailable``. For
the email to still be in the queue when this script reads it, the
notification worker must not have sent it yet (a sent email's body is erased).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

# Overridable so `make e2e` can follow a non-default BACKEND_PORT.
BASE = os.environ.get("AETHERIS_API_BASE", "http://127.0.0.1:8000/api/v1")
failures: list[str] = []

#: Databases this script must never connect to: the developer's working data
#: and the shared test database.
FORBIDDEN_DATABASES = frozenset({"aetheris", "aetheris_test"})
_ACTIVATION_LINK = re.compile(r"/reset-password#token=([A-Za-z0-9_\-]+)")


class DatabaseStepRefusedError(Exception):
    """The step that reads the queued email cannot run as configured."""


def e2e_database_url() -> str:
    """Return ``E2E_DB_URL`` after checking it is safe to connect to.

    :raises DatabaseStepRefusedError: If the variable is unset, is not a plain
        ``postgresql://`` URL, or names a database this script must not touch.
    """
    raw = os.environ.get("E2E_DB_URL", "").strip()
    if not raw:
        raise DatabaseStepRefusedError(
            "E2E_DB_URL is not set. The activation link is no longer returned by the API; "
            "this step reads it from the email queued in the backend's database. Set "
            "E2E_DB_URL=postgresql://user@host:5432/<e2e database> (never 'aetheris' or "
            "'aetheris_test')."
        )
    parts = urllib.parse.urlsplit(raw)
    if parts.scheme != "postgresql":
        raise DatabaseStepRefusedError(
            "E2E_DB_URL must be a plain postgresql:// URL "
            f"(got scheme {parts.scheme or 'none'!r}; SQLAlchemy forms such as "
            "postgresql+asyncpg:// are not accepted)."
        )
    if parts.query or parts.fragment:
        # A query string can name a different database than the path does.
        raise DatabaseStepRefusedError(
            "E2E_DB_URL must not carry a query string or fragment: the database is taken "
            "from the path only."
        )
    database = urllib.parse.unquote(parts.path.lstrip("/"))
    if not database or "/" in database:
        raise DatabaseStepRefusedError("E2E_DB_URL must name exactly one database in its path.")
    if database.lower() in FORBIDDEN_DATABASES:
        raise DatabaseStepRefusedError(
            f"E2E_DB_URL points at {database!r}. This script refuses to connect to "
            "'aetheris' or 'aetheris_test'; use a disposable end-to-end database."
        )
    return raw


async def _read_emailed_token(url: str, email: str) -> str:
    try:
        import asyncpg  # the backend's own driver; not a new dependency
    except ImportError as exc:
        raise DatabaseStepRefusedError(
            "asyncpg is not importable. Run this script with the backend's Python "
            "environment (it is already a backend dependency)."
        ) from exc

    connection = await asyncpg.connect(url, timeout=10)
    try:
        # Check what we are really connected to, whatever the URL said.
        actual = await connection.fetchval("SELECT current_database()")
        if str(actual).lower() in FORBIDDEN_DATABASES:
            raise DatabaseStepRefusedError(
                f"Connected to {actual!r}; refusing to read from 'aetheris' or 'aetheris_test'."
            )
        async with connection.transaction(readonly=True):
            bodies = await connection.fetch(
                "SELECT body FROM notification_deliveries "
                "WHERE lower(to_address) = lower($1) ORDER BY created_at DESC",
                email,
            )
    finally:
        await connection.close()

    if not bodies:
        raise DatabaseStepRefusedError(
            f"No email is queued for {email} in this database. Is E2E_DB_URL the database "
            "the backend is using, and does the backend have SMTP_HOST set?"
        )
    for row in bodies:
        match = _ACTIVATION_LINK.search(row["body"] or "")
        if match:
            return match.group(1)
    raise DatabaseStepRefusedError(
        f"An email for {email} exists but its body no longer holds a link: the worker "
        "has already sent it (a sent email's body is erased). Run against a backend whose "
        "notification worker is stopped, or whose SMTP host does not accept mail."
    )


def emailed_activation_token(email: str) -> str:
    """Read the activation token from the email queued for ``email``.

    This is the invited person's view: what is in their mailbox. Nothing the
    API returned is used.

    :raises DatabaseStepRefusedError: If the step may not or cannot run.
    """
    return asyncio.run(_read_emailed_token(e2e_database_url(), email))


def call(
    method: str,
    path: str,
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
        },
    )
    try:
        with urllib.request.urlopen(req) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except json.JSONDecodeError:
            # Some error responses (e.g. a proxy's 502) carry no JSON body.
            return e.code, {}


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def main() -> int:
    original = "Prof!Passw0rd123"
    changed = "Prof!Passw0rd456"

    # ── Set up a fresh, ordinary account ────────────────────────────────────
    status, body = call(
        "POST",
        "/auth/login",
        body={"email": "admin@demohospital.com", "password": "Admin@1234567"},
    )
    check("admin login 200", status == 200, str(body)[:300])
    admin_token = body.get("data", {}).get("access_token")
    if not admin_token:
        print("\ncannot continue without an admin token")
        return 1

    email = f"profile-{uuid.uuid4().hex[:10]}@demohospital.com"
    status, body = call(
        "POST",
        "/users",
        admin_token,
        {"email": email, "first_name": "Profile", "last_name": "Tester"},
    )
    check("invite fresh user 201", status == 201, str(body)[:300])
    invited = body.get("data", {})
    # The activation link is a credential for the new account. It is emailed
    # to the invited address and must never come back to the inviter.
    check("invite response has NO invite_token", "invite_token" not in invited,
          "the API returned an activation token to the inviter")
    check("invite response does not carry an activation link",
          "token=" not in json.dumps(body) and "reset-password" not in json.dumps(body))
    delivery = (invited.get("invitation") or {}).get("delivery")
    check("invite reports invitation.delivery",
          delivery in ("queued", "unavailable"), f"invitation={invited.get('invitation')!r}")
    if delivery != "queued":
        print(
            "\ncannot continue: the invitation was not queued "
            f"(delivery={delivery!r}). The backend needs a mail transport (SMTP_HOST) "
            "for an activation link to exist at all."
        )
        return 1

    # Activate as the invited person would: with the link from their email.
    try:
        emailed_token = emailed_activation_token(email)
    except DatabaseStepRefusedError as refusal:
        print(f"\ncannot continue: {refusal}")
        return 1
    except Exception as exc:  # noqa: BLE001 — any connection failure ends the run the same way
        print(f"\ncannot continue: reading the queued email failed ({type(exc).__name__}).")
        return 1
    check("the queued email carries an activation link", bool(emailed_token))

    status, body = call(
        "POST", "/auth/password/reset", body={"token": emailed_token, "new_password": original}
    )
    check("activate via the emailed link 200", status == 200, str(body)[:300])
    status, _ = call(
        "POST", "/auth/password/reset", body={"token": emailed_token, "new_password": changed}
    )
    check("the emailed link works only once (401)", status == 401, f"status={status}")

    status, body = call("POST", "/auth/login", body={"email": email, "password": original})
    check("activated user can log in 200", status == 200, str(body)[:300])
    token = body.get("data", {}).get("access_token")
    if not token:
        print("\ncannot continue without a user token")
        return 1

    # ── The page's initial load ─────────────────────────────────────────────
    status, body = call("GET", "/users/me", token)
    check("GET /users/me 200", status == 200, str(body)[:300])
    me = body.get("data", {})
    check("profile carries the identity fields the page renders",
          me.get("email") == email and me.get("first_name") == "Profile",
          str(me)[:200])
    check("profile exposes mfa_enabled for the MFA card", "mfa_enabled" in me)

    # ── Editing name and phone ──────────────────────────────────────────────
    status, body = call(
        "PATCH",
        "/users/me",
        token,
        {"first_name": "Profile", "last_name": "Tester-Edited", "phone": "+919812345678"},
    )
    check("PATCH /users/me 200", status == 200, str(body)[:300])
    updated = body.get("data", {})
    check("last name persisted", updated.get("last_name") == "Tester-Edited",
          str(updated.get("last_name")))
    check("phone persisted", updated.get("phone") == "+919812345678", str(updated.get("phone")))

    status, body = call("GET", "/users/me", token)
    check("edit survives a reload", body.get("data", {}).get("last_name") == "Tester-Edited")

    status, _ = call("PATCH", "/users/me", token, {"phone": "not-a-number"})
    check("malformed phone rejected 422", status == 422, f"status={status}")

    # ── The profile page needs no user.* permission ─────────────────────────
    status, _ = call("GET", "/users", token)
    check("role-less user cannot reach the admin directory (403)", status == 403, f"status={status}")
    status, _ = call("GET", "/users/me", token)
    check("...but can still read their own profile (200)", status == 200, f"status={status}")

    # ── Password change ─────────────────────────────────────────────────────
    status, _ = call(
        "POST",
        "/auth/password/change",
        token,
        {"current_password": "Wrong!Passw0rd1", "new_password": changed},
    )
    check("wrong current password rejected 401", status == 401, f"status={status}")

    status, body = call(
        "POST",
        "/auth/password/change",
        token,
        {"current_password": original, "new_password": changed},
    )
    check("password change 200", status == 200, str(body)[:300])

    status, _ = call("POST", "/auth/login", body={"email": email, "password": original})
    check("old password no longer works", status != 200, f"status={status}")

    status, body = call("POST", "/auth/login", body={"email": email, "password": changed})
    check("new password logs in 200", status == 200, str(body)[:300])
    token = body.get("data", {}).get("access_token", token)

    # ── MFA enrollment seam ─────────────────────────────────────────────────
    status, _ = call("POST", "/auth/mfa/enroll", token, {"password": "Wrong!Passw0rd1"})
    check("MFA enroll with wrong password rejected 401", status == 401, f"status={status}")

    status, body = call("POST", "/auth/mfa/enroll", token, {"password": changed})
    check("MFA enroll 200", status == 200, str(body)[:300])
    secret = body.get("data", {}).get("secret")
    check("enroll returns a TOTP secret the card displays", bool(secret))

    status, _ = call("POST", "/auth/mfa/confirm", token, {"secret": secret, "code": "000000"})
    check("MFA confirm with a bad code rejected", status in (400, 401), f"status={status}")

    status, body = call("GET", "/users/me", token)
    check("MFA stays off until a valid code confirms it",
          body.get("data", {}).get("mfa_enabled") is False,
          str(body.get("data", {}).get("mfa_enabled")))

    print()
    if failures:
        print(f"{len(failures)} CHECK(S) FAILED: {', '.join(failures)}")
        return 1
    print("ALL PROFILE E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
