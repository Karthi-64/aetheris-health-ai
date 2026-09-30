"""Sprint 3 E2E: hospital settings, audit trail, demo-role RBAC matrix.

Exercises the NEW modules against the real running backend (no test
fixtures): the Settings API, the durable audit sink (including whether login
events actually persist through the request lifecycle), the audit search
filters/export gate, and the 20-day plan's "test each demo role against
permitted and forbidden actions" (Days 18–20).

Usage::

    cd backend && ../.venv/bin/python ../scripts/e2e_sprint3.py
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

BASE = os.environ.get("AETHERIS_API_BASE", "http://127.0.0.1:8000/api/v1")

failures: list[str] = []


def call(
    method: str,
    path: str,
    token: str | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    """Issue one request and return (status, parsed body)."""
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
            return e.code, {}


def check(name: str, ok: bool, detail: str = "") -> None:
    """Record one expectation."""
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  — {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def login(email: str, password: str) -> tuple[str, list[str]]:
    """Return (access_token, permissions) for a demo account."""
    status, body = call("POST", "/auth/login", body={"email": email, "password": password})
    data = body.get("data", {})
    return data.get("access_token", ""), data.get("user", {}).get("permissions", []) or []


def main() -> int:
    stamp = int(time.time())

    # ── 1. Admin session ──────────────────────────────────────────────────
    admin_token, admin_perms = login("admin@demohospital.com", "Admin@1234567")
    check("admin login", bool(admin_token))
    check(
        "admin holds settings.read + settings.update",
        "settings.read" in admin_perms and "settings.update" in admin_perms,
        str(admin_perms),
    )
    check("admin holds audit.read", "audit.read" in admin_perms, str(admin_perms))
    check(
        "admin lacks audit.export (Super Admin only, spec 12 §10)",
        "audit.export" not in admin_perms,
        str(admin_perms),
    )

    # ── 2. Hospital settings ──────────────────────────────────────────────
    status, body = call("GET", "/hospitals/current", token=admin_token)
    check("GET /hospitals/current 200", status == 200, str(status))
    check("public view carries the hospital name", bool(body.get("data", {}).get("name")))
    check(
        "public view hides internal settings",
        "settings" not in body.get("data", {}),
        str(list(body.get("data", {}).keys())),
    )

    status, body = call("GET", "/hospitals/current/full", token=admin_token)
    check("GET /hospitals/current/full 200", status == 200, str(status))
    original_name = body.get("data", {}).get("name", "")
    check(
        "full view has restricted fields (slug/timezone)",
        bool(body.get("data", {}).get("slug")) and bool(body.get("data", {}).get("timezone")),
    )

    e2e_name = f"E2E Hospital {stamp}"
    status, body = call(
        "PATCH", "/hospitals/current", token=admin_token, body={"name": e2e_name}
    )
    check("PATCH settings 200", status == 200, str(body)[:300])
    check("PATCH returns new name", body.get("data", {}).get("name") == e2e_name)

    status, body = call("GET", "/hospitals/current/full", token=admin_token)
    check("update persisted", body.get("data", {}).get("name") == e2e_name)

    status, _ = call("PATCH", "/hospitals/current", token=admin_token, body={"timezone": "UTC"})
    check("restricted field timezone → 422", status == 422, str(status))
    status, _ = call("PATCH", "/hospitals/current", token=admin_token, body={"slug": "hacked"})
    check("restricted field slug → 422", status == 422, str(status))
    status, _ = call(
        "PATCH", "/hospitals/current", token=admin_token, body={"is_superuser": True}
    )
    check("unknown field → 422", status == 422, str(status))

    status, _ = call(
        "PATCH", "/hospitals/current", token=admin_token, body={"name": original_name}
    )
    check("revert name 200", status == 200, str(status))

    # ── 3. Durable audit trail ────────────────────────────────────────────
    status, body = call("GET", "/audit-logs?page_size=100", token=admin_token)
    check("GET /audit-logs 200", status == 200, str(status)[:300])
    rows = body.get("data", [])
    actions = {r["action"] for r in rows}
    check(
        "settings update landed in the durable trail",
        "settings.hospital_updated" in actions,
        str(sorted(actions)),
    )
    check(
        "login events persist through the request lifecycle",
        "auth.login.success" in actions,
        str(sorted(actions)),
    )
    entry = next((r for r in rows if r["action"] == "settings.hospital_updated"), None)
    if entry:
        check("audit entry resolves the actor", bool(entry.get("actor_name")), str(entry))
        check(
            "audit entry carries a before/after diff",
            isinstance(entry.get("before"), dict) and isinstance(entry.get("after"), dict),
            str(entry)[:200],
        )
        status, _ = call("GET", f"/audit-logs/{entry['id']}", token=admin_token)
        check("GET /audit-logs/{id} 200", status == 200, str(status))
    else:
        check("audit entry found for detail fetch", False, "no settings entry")

    status, body = call(
        "GET", "/audit-logs?action=settings.hospital_updated&page_size=50", token=admin_token
    )
    filtered = body.get("data", [])
    check(
        "action filter is exact",
        status == 200 and all(r["action"] == "settings.hospital_updated" for r in filtered)
        and len(filtered) > 0,
        f"status={status} n={len(filtered)}",
    )

    status, _ = call("GET", "/audit-logs?q=ab", token=admin_token)
    check("q under 3 chars → 422", status == 422, str(status))
    status, body = call("GET", "/audit-logs?q=login", token=admin_token)
    check(
        "q ≥3 chars searches actions",
        status == 200 and all("login" in r["action"] for r in body.get("data", [])),
        str(status),
    )

    year_ago = time.strftime("%Y-%m-%dT00:00:00+00:00", time.gmtime(stamp - 400 * 86400))
    now = time.strftime("%Y-%m-%dT00:00:00+00:00", time.gmtime(stamp))
    status, _ = call("GET", f"/audit-logs?from={year_ago}&to={now}", token=admin_token)
    check("date range > 1 year → 422", status == 422, str(status))

    status, _ = call("GET", "/audit-logs/export?format=csv", token=admin_token)
    check("export denied without audit.export → 403", status == 403, str(status))

    status, _ = call("GET", "/audit-logs")
    check("anonymous audit read → 401", status == 401, str(status))

    # ── 4. Demo-role RBAC matrix (Days 18–20) ─────────────────────────────
    rec_token, rec_perms = login("reception@demohospital.com", "Reception@1234567")
    check("receptionist login", bool(rec_token))
    check("receptionist holds patient.read", "patient.read" in rec_perms, str(rec_perms))

    status, _ = call("GET", "/patients?page_size=1", token=rec_token)
    check("receptionist GET /patients → 200", status == 200, str(status))
    status, _ = call("GET", "/hospitals/current", token=rec_token)
    check("receptionist public hospital view → 200", status == 200, str(status))
    status, _ = call("GET", "/users", token=rec_token)
    check("receptionist GET /users → 403", status == 403, str(status))
    status, _ = call("GET", "/audit-logs", token=rec_token)
    check("receptionist GET /audit-logs → 403", status == 403, str(status))
    status, _ = call("GET", "/hospitals/current/full", token=rec_token)
    check("receptionist GET settings/full → 403", status == 403, str(status))
    status, _ = call(
        "PATCH", "/hospitals/current", token=rec_token, body={"name": "Sneaky Hospital"}
    )
    check("receptionist PATCH settings → 403", status == 403, str(status))

    doc_token, doc_perms = login("doctor@demohospital.com", "Doctor@1234567")
    check("doctor login", bool(doc_token))
    status, _ = call("GET", "/doctors?page_size=1", token=doc_token)
    check("doctor GET /doctors → 200", status == 200, str(status))
    status, _ = call("GET", "/users", token=doc_token)
    check("doctor GET /users → 403", status == 403, str(status))
    status, _ = call("GET", "/audit-logs", token=doc_token)
    check("doctor GET /audit-logs → 403", status == 403, str(status))
    status, _ = call("GET", "/hospitals/current/full", token=doc_token)
    check("doctor GET settings/full → 403", status == 403, str(status))
    check(
        "doctor holds no user/audit/settings codes",
        not ({"user.read", "audit.read", "settings.read"} & set(doc_perms)),
        str(doc_perms),
    )

    status, _ = call("GET", "/users?page_size=1", token=admin_token)
    check("admin GET /users → 200 (positive control)", status == 200, str(status))

    print()
    if failures:
        print(f"{len(failures)} FAILED:")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
