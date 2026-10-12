"""E2E: AI slot recommendation against the real running backend.

Run by a person after ``make migrate`` and ``make seed``, with the backend
started from ``backend/`` and a Groq key configured there. It exercises the
whole path — capability read, the slots feed, the recommendation, and the
booking a member of staff then makes — and checks that the recommendation
itself writes nothing.

It logs in as seeded demo accounts (development data from ``make seed``) and
prints PASS/FAIL per check. It prints no token, and nothing about the AI key:
that the model really answered is confirmed in the backend log, in the
``ai_interaction`` line.

Usage::

    cd backend && uv run python ../scripts/e2e_ai_slot_recommendation.py
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import date, datetime, timedelta
from typing import Any

BASE = os.environ.get("AETHERIS_API_BASE", "http://127.0.0.1:8000/api/v1")
FLAG = "feature.ai.slot_recommendation"

# Seeded demo accounts (backend/app/seeds/seed.py). Development data only.
RECEPTION = ("reception@demohospital.com", "Reception@1234567")
DOCTOR = ("doctor@demohospital.com", "Doctor@1234567")
ADMIN = ("admin@demohospital.com", "Admin@1234567")

failures: list[str] = []


def call(
    method: str,
    path: str,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Issue one request and return (status, parsed body)."""
    req = urllib.request.Request(
        f"{BASE}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {token}"} if token else {}),
            **(headers or {}),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
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


def login(account: tuple[str, str]) -> str:
    """Return an access token for a seeded demo account."""
    _status, body = call("POST", "/auth/login", body={"email": account[0], "password": account[1]})
    return str(body.get("data", {}).get("access_token", ""))


def total(path: str, token: str) -> int:
    """Total records of a paginated list."""
    _status, body = call("GET", path, token=token)
    return int(body.get("metadata", {}).get("pagination", {}).get("total_records", -1))


def next_weekday(after: date) -> date:
    """The next Monday–Friday strictly after ``after``."""
    day = after + timedelta(days=1)
    while day.weekday() > 4:
        day += timedelta(days=1)
    return day


def main() -> int:
    reception = login(RECEPTION)
    admin = login(ADMIN)
    check("receptionist login", bool(reception))
    check("admin login", bool(admin))
    if not reception or not admin:
        return 1

    # ── Capability ────────────────────────────────────────────────────────
    status, body = call("GET", "/hospitals/current/feature-flags", token=reception)
    flags = body.get("data", {}).get("flags", {})
    check("GET /hospitals/current/feature-flags 200", status == 200, str(status))
    check(
        "slot recommendation is available (flag seeded on AND server AI configured)",
        flags.get(FLAG) == {"available": True},
        str(flags),
    )

    # ── A doctor, a patient, and a weekday with free slots ────────────────
    _status, body = call("GET", "/doctors?page_size=50", token=reception)
    doctors = body.get("data", [])
    _status, body = call("GET", "/patients?page_size=1", token=reception)
    patients = body.get("data", [])
    check("a seeded doctor exists", bool(doctors))
    check("a seeded patient exists", bool(patients))
    if not doctors or not patients:
        return 1
    patient_id = patients[0]["id"]

    _status, body = call("GET", "/hospitals/current", token=reception)
    timezone = body.get("data", {}).get("timezone", "?")

    doctor_id = ""
    day = date.today()
    free: list[dict[str, Any]] = []
    for candidate in doctors:
        probe = date.today()
        for _ in range(7):
            probe = next_weekday(probe)
            _status, body = call(
                "GET", f"/doctors/{candidate['id']}/slots?date={probe.isoformat()}", token=reception
            )
            slots = body.get("data", {}).get("slots", [])
            available = [slot for slot in slots if slot.get("status") == "available"]
            if available:
                doctor_id, day, free = candidate["id"], probe, available
                break
        if doctor_id:
            break
    check("a seeded doctor has available slots on a coming weekday", bool(doctor_id))
    if not doctor_id:
        return 1
    print(f"      doctor {doctor_id} on {day.isoformat()} ({timezone}): {len(free)} free slots")

    appointments_path = f"/appointments?patient_id={patient_id}&page_size=1"
    appointments_before = total(appointments_path, reception)
    audit_before = total("/audit-logs?page_size=1", admin)

    # ── The recommendation ────────────────────────────────────────────────
    request = {"patient_id": patient_id, "doctor_id": doctor_id, "date": day.isoformat()}
    status, body = call("POST", "/appointments/recommend-slot", token=reception, body=request)
    data = body.get("data") or {}
    recommendation = data.get("recommendation") or {}
    check(
        "POST /appointments/recommend-slot 200", status == 200, f"{status} {body.get('error_code')}"
    )
    check("status is 'recommended'", data.get("status") == "recommended", str(data.get("status")))
    check(
        "response has no provider, model, score or confidence",
        not ({"provider", "model", "score", "confidence"} & (set(data) | set(recommendation))),
    )
    check(
        "candidate_count equals the picker's future available slots",
        data.get("candidate_count")
        == len(
            [s for s in free if datetime.fromisoformat(s["start"]) > datetime.now().astimezone()]
        ),
        str(data.get("candidate_count")),
    )
    if status != 200 or not recommendation:
        return 1

    chosen = (
        datetime.fromisoformat(recommendation["slot_start"]),
        datetime.fromisoformat(recommendation["slot_end"]),
    )
    available_instants = {
        (datetime.fromisoformat(s["start"]), datetime.fromisoformat(s["end"])) for s in free
    }
    check("the suggested slot is one the picker shows as available", chosen in available_instants)
    print(f"      suggested {recommendation['slot_start']}")

    check(
        "the recommendation created no appointment",
        total(appointments_path, reception) == appointments_before,
    )
    check(
        "the recommendation created no audit entry",
        total("/audit-logs?page_size=1", admin) == audit_before,
    )

    # ── The human books ───────────────────────────────────────────────────
    status, body = call(
        "POST",
        "/appointments",
        token=reception,
        body={
            "patient_id": patient_id,
            "doctor_id": doctor_id,
            "scheduled_start": recommendation["slot_start"],
            "scheduled_end": recommendation["slot_end"],
            "type": "new",
            "reason": "E2E: booked by staff after an AI suggestion",
        },
        headers={"Idempotency-Key": f"e2e-ai-{uuid.uuid4().hex}"},
    )
    booked = body.get("data") or {}
    check("POST /appointments 201", status == 201, f"{status} {body.get('message')}")
    check(
        "the appointment is listed once",
        total(appointments_path, reception) == appointments_before + 1,
    )
    query = urllib.parse.urlencode({"action": "appointment.booked", "page_size": 20})
    _status, body = call("GET", f"/audit-logs?{query}", token=admin)
    booked_entries = [
        entry for entry in body.get("data", []) if entry.get("target_id") == booked.get("id")
    ]
    check("the booking is in the audit trail as appointment.booked", len(booked_entries) == 1)

    # ── Who may not ask ───────────────────────────────────────────────────
    doctor = login(DOCTOR)
    status, body = call("POST", "/appointments/recommend-slot", token=doctor, body=request)
    check(
        "the doctor role is refused with PERMISSION_DENIED",
        status == 403 and body.get("error_code") == "PERMISSION_DENIED",
        f"{status} {body.get('error_code')}",
    )
    status, _body = call("POST", "/appointments/recommend-slot", body=request)
    check("no token is 401", status == 401, str(status))

    print()
    print("All checks passed." if not failures else f"{len(failures)} check(s) FAILED.")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
