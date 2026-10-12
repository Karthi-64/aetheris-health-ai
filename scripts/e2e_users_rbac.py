"""End-to-end verification of Sprint 2 user management & RBAC.

Drives the real running backend (uvicorn on :8000) over HTTP:
  1. Login as the seeded Hospital Admin — assert the payload now carries
     `permissions` and `name` (the SPA's RBAC inputs).
  2. GET /users — list renders with pagination metadata.
  3. POST /users — invite a new user with a role. The response must NOT
     contain the activation token (it is a credential for the new account and
     goes only to the invited mailbox); it reports `invitation.delivery`.
     POST /users/{id}/invitation — sending it again returns no token either.
  4. GET /roles — the assignable catalog.
  5. POST /users/{id}/roles — assign another role.
  6. DELETE /users/{id}/roles/{role_id} — remove it again.
  7. Login as the seeded Receptionist — assert the *denied* path: no
     user-management permissions, and /users returns 403.
  8. Deactivate + reactivate the invited user. The user never activated the
     account, so reactivation returns it to `invited`, not `active`.

This script never activates the invited user, so it needs no access to the
queued email and no database connection.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

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


def token_like_keys(value: object) -> list[str]:
    """Every key, at any depth, whose name suggests a credential."""
    found: list[str] = []
    if isinstance(value, dict):
        for key, inner in value.items():
            if "token" in str(key).lower():
                found.append(str(key))
            found.extend(token_like_keys(inner))
    elif isinstance(value, list):
        for inner in value:
            found.extend(token_like_keys(inner))
    return found


def main() -> int:
    # Unique mailbox per run: a suspended user from a previous run must never
    # trip the (correct) duplicate-email 409.
    stamp = int(time.time())
    invite_email = f"e2e.nurse.{stamp}@demohospital.com"

    # ── 1. Admin login ────────────────────────────────────────────────────
    status, body = call("POST", "/auth/login", body={
        "email": "admin@demohospital.com",
        "password": "Admin@1234567",
    })
    check("admin login 200", status == 200, str(body)[:300])
    user = body.get("data", {}).get("user", {})
    admin_token = body.get("data", {}).get("access_token", "")

    perms = user.get("permissions")
    check("login payload has permissions array", isinstance(perms, list) and len(perms) > 0,
          f"permissions={perms}")
    check("login payload has display name", user.get("name") == "Admin User", f"name={user.get('name')!r}")
    check("admin holds user.read", isinstance(perms, list) and "user.read" in perms)
    check("admin holds role.assign", isinstance(perms, list) and "role.assign" in perms)
    # The backend catalog has no dashboard.view code — the Dashboard is the SPA
    # home page, reachable by any authenticated user holding at least one permission.
    check("admin holds a non-empty permission set (drives the home page)",
          isinstance(perms, list) and len(perms) > 0)

    # ── 2. List users ─────────────────────────────────────────────────────
    status, body = call("GET", "/users?page=1&page_size=25", token=admin_token)
    check("GET /users 200", status == 200, str(body)[:300])
    items = body.get("data", [])
    pagination = body.get("metadata", {}).get("pagination", {})
    check("users list is an array", isinstance(items, list) and len(items) >= 3, f"n={len(items)}")
    check("pagination metadata present",
          {"page", "page_size", "total_records", "total_pages"} <= set(pagination), str(pagination))

    # ── 3. Roles catalog ──────────────────────────────────────────────────
    status, body = call("GET", "/roles?page_size=100", token=admin_token)
    check("GET /roles 200", status == 200, str(body)[:300])
    roles = body.get("data", [])
    check("roles catalog seeded", isinstance(roles, list) and len(roles) >= 5, f"n={len(roles)}")
    role_by_name = {r["name"]: r["id"] for r in roles}
    check("Hospital Admin role exists", "Hospital Admin" in role_by_name)
    check("Doctor role exists", "Doctor" in role_by_name)

    # ── 4. Invite a user with a role ──────────────────────────────────────
    status, body = call("POST", "/users", token=admin_token, body={
        "email": invite_email,
        "first_name": "E2E",
        "last_name": "Nurse",
        "role_ids": [role_by_name["Nurse"]] if "Nurse" in role_by_name else [],
    })
    check("POST /users 201", status == 201, str(body)[:300])
    invited = body.get("data", {})
    invite_id = invited.get("id", "")
    check("invited user status is invited", invited.get("status") == "invited",
          f"status={invited.get('status')}")
    # The activation link is a credential for the invited account. It is
    # emailed to the invited address and must never come back to the caller.
    check("invite response has NO invite_token", "invite_token" not in invited,
          "the API returned an activation token to the inviter")
    check("invite response has no token-like key anywhere",
          token_like_keys(body) == [], str(token_like_keys(body)))
    check("invite response does not carry an activation link",
          "token=" not in json.dumps(body) and "reset-password" not in json.dumps(body))
    delivery = (invited.get("invitation") or {}).get("delivery")
    check("invite reports invitation.delivery",
          delivery in ("queued", "unavailable"), f"invitation={invited.get('invitation')!r}")
    check("invited user carries assigned role",
          any(r["name"] == "Nurse" for r in invited.get("roles", [])),
          str(invited.get("roles")))

    # Duplicate invite must 409
    status, body = call("POST", "/users", token=admin_token, body={
        "email": invite_email, "first_name": "E2E", "last_name": "Nurse",
    })
    check("duplicate invite 409", status == 409, f"status={status}")

    # Sending the invitation again: same rule, no token for the caller.
    status, body = call("POST", f"/users/{invite_id}/invitation", token=admin_token)
    check("resend invitation 200", status == 200, str(body)[:300])
    check("resend reports the same delivery state",
          body.get("data") == {"delivery": delivery}, str(body.get("data")))
    check("resend response has no token-like key", token_like_keys(body) == [],
          str(token_like_keys(body)))
    check("resend response does not carry an activation link",
          "token=" not in json.dumps(body) and "reset-password" not in json.dumps(body))

    # ── 5. Assign another role ────────────────────────────────────────────
    status, body = call("POST", f"/users/{invite_id}/roles", token=admin_token,
                        body={"role_id": role_by_name["Doctor"]})
    check("assign Doctor role 200", status == 200, str(body)[:300])

    status, body = call("GET", f"/users/{invite_id}/roles", token=admin_token)
    names = {r["name"] for r in body.get("data", [])}
    check("user now holds Nurse + Doctor", {"Nurse", "Doctor"} <= names, str(names))

    # ── 6. Remove the role again ──────────────────────────────────────────
    status, body = call("DELETE", f"/users/{invite_id}/roles/{role_by_name['Doctor']}", token=admin_token)
    check("remove Doctor role 200", status == 200, str(body)[:300])
    status, body = call("GET", f"/users/{invite_id}/roles", token=admin_token)
    names = {r["name"] for r in body.get("data", [])}
    check("Doctor role removed", "Doctor" not in names and "Nurse" in names, str(names))

    # ── 7. Denied path — receptionist lacks user-management permissions ──
    status, body = call("POST", "/auth/login", body={
        "email": "reception@demohospital.com",
        "password": "Reception@1234567",
    })
    check("receptionist login 200", status == 200, str(body)[:300])
    rec_token = body.get("data", {}).get("access_token", "")
    rec_perms = body.get("data", {}).get("user", {}).get("permissions", [])
    status, _ = call("POST", f"/users/{invite_id}/invitation", token=rec_token)
    check("receptionist cannot resend an invitation (403)", status == 403, f"status={status}")
    check("receptionist lacks user.read", "user.read" not in rec_perms, str(rec_perms))
    check("receptionist lacks role.assign", "role.assign" not in rec_perms)

    status, body = call("GET", "/users", token=rec_token)
    check("receptionist GET /users → 403", status == 403, f"status={status}")
    status, body = call("POST", "/users", token=rec_token, body={
        "email": "sneaky@demohospital.com", "first_name": "S", "last_name": "N",
    })
    check("receptionist POST /users → 403", status == 403, f"status={status}")

    # ── 8. Deactivate / reactivate lifecycle ─────────────────────────────
    status, body = call("POST", f"/users/{invite_id}/deactivate", token=admin_token)
    check("deactivate 200", status == 200, str(body)[:300])
    check("status now suspended", body.get("data", {}).get("status") == "suspended",
          str(body.get("data", {}).get("status")))

    status, body = call("POST", f"/users/{invite_id}/reactivate", token=admin_token)
    check("reactivate 200", status == 200, str(body)[:300])
    # Nobody ever set a password on this account, so it goes back to waiting
    # for its invitation. "active" here would be an account with no owner.
    check("never-activated user returns to invited (not active)",
          body.get("data", {}).get("status") == "invited",
          str(body.get("data", {}).get("status")))
    check("reactivate response has no token-like key", token_like_keys(body) == [],
          str(token_like_keys(body)))

    # Admin cannot deactivate themselves (module spec rule 7)
    admin_id = user.get("id", "")
    status, body = call("POST", f"/users/{admin_id}/deactivate", token=admin_token)
    check("self-deactivation blocked 400", status == 400, f"status={status}")

    print()
    if failures:
        print(f"{len(failures)} FAILURES: {failures}")
        return 1
    print("ALL E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
