"""End-to-end verification of the self-service profile page.

Drives the real running backend (uvicorn on :8000) over HTTP, exercising the
endpoints the profile screen calls — module spec ``02-user-management.md`` §12
("Own profile page"):

  1. Invite a fresh user as the admin, then activate them with the invite
     token, so the flows run as an ordinary low-privilege account.
  2. GET  /users/me            — the page's initial load.
  3. PATCH /users/me           — name and phone.
  4. PATCH /users/me           — a malformed phone is rejected (422).
  5. Assert the account cannot reach the admin directory (403), proving the
     profile page needs no ``user.*`` permission.
  6. POST /auth/password/change — wrong current password is rejected, the
     right one succeeds, and the new password actually logs in.
  7. POST /auth/mfa/enroll → /mfa/confirm is refused with a bad code.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
import uuid

# Overridable so `make e2e` can follow a non-default BACKEND_PORT.
BASE = os.environ.get("AETHERIS_API_BASE", "http://127.0.0.1:8000/api/v1")
failures: list[str] = []


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
    invite_token = body.get("data", {}).get("invite_token")
    check("invite returns a token", bool(invite_token))

    status, body = call(
        "POST", "/auth/password/reset", body={"token": invite_token, "new_password": original}
    )
    check("activate via invite token 200", status == 200, str(body)[:300])

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
