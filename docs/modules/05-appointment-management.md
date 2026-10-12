# 05 — Appointment Management

**Owner:** TBD
**Phase:** MVP
**Status:** Approved

---

## 1. Purpose

Coordinate the meeting between patient and doctor. Own the appointment lifecycle from booking through completion, including walk-ins, cancellations, and no-shows. Provide the schedule the entire clinic runs on for the day.

## 2. Scope

### In Scope

- Book / reschedule / cancel appointments
- Walk-in queue
- Appointment status lifecycle: booked → checked-in → in-progress → completed → no-show / cancelled
- Doctor calendar view
- Reception dashboard for the day
- AI slot recommendation — optional and advisory: one suggested slot that a member of staff reviews and books (Section 5.9)
- AI-generated reminder text — **not built**; future (Section 13)
- Immutable status history

### Out of Scope

- Clinical notes → Consultation
- Fees / invoicing → Billing (created at completion)
- Notifications delivery → Notification service
- Slot computation → Doctor Management

## 3. Personas & Permissions

| Role | Can do |
|---|---|
| Receptionist | Book, reschedule, cancel, check in, no-show, view schedule |
| Doctor | View own schedule, start / complete own appointments |
| Nurse | View schedule, check in patients |
| Hospital Admin | All operations |
| Patient (portal, future) | View own appointments, request reschedule |

## 4. Business Rules

1. An appointment must reference a valid patient and doctor in the same hospital.
2. `scheduled_start < scheduled_end`; duration must match a doctor's slot duration.
3. Two appointments cannot overlap for the same doctor (unless both are walk-in and admin overrides).
4. Booking outside doctor availability requires `appointment.book_override` permission.
5. A cancelled appointment cannot be reactivated; book a new one instead.
6. Status transitions follow the state machine (Section 5.1); invalid transitions return 400.
7. Every status change writes to `appointment_status_history`.
8. Idempotency key required on `POST /appointments` to prevent double-book on client retry.
9. A completed appointment cannot be edited except to append clinical notes (owned by Consultation).

## 5. Workflow

### 5.1 State Machine

```
             ┌─────────┐
   book    → │ booked  │ ─ cancel → cancelled
             └────┬────┘
                  │ check-in
                  ▼
             ┌──────────┐
             │checked_in│ ─ cancel → cancelled
             └────┬─────┘
                  │ start
                  ▼
             ┌──────────┐
             │in_progress│
             └────┬─────┘
                  │ complete
                  ▼
             ┌──────────┐
             │ completed│
             └──────────┘

Any state other than completed / cancelled can transition to no_show if scheduled_end passes without check-in.
```

### 5.2 Book (happy path)

1. Reception picks patient + doctor + date.
2. Client calls `GET /doctors/{id}/slots?date=...` to get available slots.
3. Reception picks a slot. Optionally they first ask for an AI suggestion (Section 5.9), which only pre-selects a slot in the same picker — the steps below are identical either way.
4. `POST /appointments` with `Idempotency-Key`.
5. Service checks: patient exists, doctor available, no overlap, slot inside availability (unless override).
6. Transaction: insert appointment + insert status history row.
7. Notification: appointment confirmation.
8. Audit: `appointment.booked`.

### 5.3 Reschedule

1. `PATCH /appointments/{id}` with new `scheduled_start` / `scheduled_end`.
2. Only allowed while status = `booked`.
3. Same validation as booking.
4. Notification of the change.
5. Audit + status history entry (`booked → booked` with `reason = 'reschedule'`).

### 5.4 Cancel

1. `POST /appointments/{id}/cancel` with `reason`.
2. Allowed from `booked` or `checked_in`.
3. Sets `cancelled_reason`, updates status.
4. Notification.
5. Audit.

### 5.5 Check-in

1. `POST /appointments/{id}/check-in`.
2. Requires status = `booked`.
3. Sets `checked_in_at`.

### 5.6 Start & complete

1. Doctor `POST /appointments/{id}/start` when ready.
2. Status → `in_progress`, `started_at` set.
3. Doctor completes consultation (Consultation module).
4. Doctor `POST /appointments/{id}/complete`.
5. Status → `completed`, `completed_at` set.
6. Trigger: Billing invoice draft creation via BillingService.
7. Audit.

### 5.7 No-show sweeper

- Background job runs every 5 minutes.
- For appointments with status ∈ {booked, checked_in} and `scheduled_end < now() - grace_period`, mark `no_show`.
- Grace period per hospital setting (default 30 minutes).

### 5.8 Walk-in

1. Reception `POST /appointments` with `type = 'walk_in'`.
2. Service assigns next available slot for the requested doctor.
3. If no slot in next X minutes, response asks for override / queue.
4. Queue view shows walk-ins ordered by arrival time.

### 5.9 AI slot recommendation

An optional aid inside the booking dialog. It is **advisory**: it suggests one slot, a member of staff decides, and the booking is the ordinary one from Section 5.2. It never books, reserves or changes anything. Booking by hand works the same whether or not it is available.

**Availability.** The control is offered only when all of these hold:

- the user holds `appointment.recommend_slot` **and** `doctor.availability.read`;
- the hospital's `feature.ai.slot_recommendation` flag is on;
- AI is configured on the server.

The client learns the last two from `GET /hospitals/current/feature-flags`, which returns one `available` boolean for the feature.

**Flow.**

1. Reception has chosen patient, doctor and date, and the day's slots are loaded. They click "Suggest a slot with AI".
2. Client calls `POST /appointments/recommend-slot` with `{ patient_id, doctor_id, date }` — all three required, nothing else accepted. `date` is the calendar day in the hospital's timezone.
3. The server checks the hospital flag, that AI is configured, and that the patient and the doctor belong to the hospital.
4. The server computes the candidates itself: the doctor's slots for that day from the same slot generator as `GET /doctors/{id}/slots`, keeping those that are available and have not started. The client cannot supply slots. If there are none, the answer is `no_free_slots` and no model is called.
5. The model is shown the day as clock times only — free slots under throwaway ids, the rest marked unavailable, plus the date and weekday — and is asked to choose one. It receives **no patient, doctor, hospital or user information**. One call; no retry and no fallback provider.
6. The server validates the answer: it must be well-formed and name one of the ids the server offered. The chosen slot is then re-checked against the database (still in the future, no overlapping appointment, no leave, inside availability). An answer that fails either check is rejected, not repaired.
7. The response carries one suggestion — `slot_start`, `slot_end`, `doctor_id` and a short plain-text `reason` written by the model — or `no_free_slots`. There is no ranked list and no score.
8. The UI shows the suggestion labelled as AI-suggested. "Use this slot" selects it in the slot picker; nothing is booked.
9. Reception presses Book. That is the normal `POST /appointments` (Section 5.2), with all of its own validation, audited as `appointment.booked` by that member of staff.

**Failure.** Every failure is explicit and none blocks manual booking: `403 FEATURE_DISABLED` (flag off), `503 AI_NOT_CONFIGURED`, `503 AI_PROVIDER_UNAVAILABLE`, `503 AI_PROVIDER_TIMEOUT`, `503 AI_RESPONSE_INVALID`. The exact contract is in `docs/18-API_CONTRACTS.md` §5.6–5.7.

**Not built (in the original spec, now future scope).** The first version of this section asked for `urgency` and `preferred_window` inputs, an optional `doctor_id` (searching across doctors), consideration of doctor load and patient history, and a ranked list of slots. None of that exists: the request takes no urgency or preferred window, the doctor is required, the model is not patient-aware — it sees no visit history or preferences — and one slot is returned. See Section 19.

## 6. Functional Requirements

- FR-1: The system shall book appointments with a state-machine-controlled lifecycle.
- FR-2: The system shall prevent double-booking for the same doctor.
- FR-3: The system shall support walk-in appointments.
- FR-4: The system shall support cancellation, reschedule, check-in, start, complete transitions.
- FR-5: The system shall generate an immutable status history.
- FR-6: The system shall provide an optional AI-assisted slot suggestion: one slot chosen from the server-computed free slots of one doctor's day, validated server-side, advisory only and booked only by a member of staff. Booking shall work without it.
- FR-7: The system shall auto-mark no-shows via a background job.
- FR-8: The system shall enforce idempotency on booking.

## 7. Non-Functional Requirements

- Booking p95 < 500ms.
- Slot recommendation p95 < 2s (AI) — a target; it has not been measured. What is enforced is a server-side deadline on the model call (8 seconds by default), after which the request fails with `AI_PROVIDER_TIMEOUT` and the user books by hand.
- No-show sweeper cannot lag more than 10 minutes behind real time.

## 8. Database Design

Tables `appointments`, `appointment_status_history` in `05-DATABASE_DESIGN.md`.

Constraint (deferred to Postgres `EXCLUDE` in migration):

```sql
ALTER TABLE appointments
ADD CONSTRAINT no_overlap_per_doctor
EXCLUDE USING gist (
  doctor_id WITH =,
  tstzrange(scheduled_start, scheduled_end, '[)') WITH &&
) WHERE (deleted_at IS NULL AND status NOT IN ('cancelled', 'no_show'));
```

Indexes:
- `ix_appointments_hospital_scheduled_start (hospital_id, scheduled_start)`
- `ix_appointments_doctor_scheduled_start (doctor_id, scheduled_start)`
- `ix_appointments_patient_scheduled_start (patient_id, scheduled_start DESC)`
- `ix_appointments_status (hospital_id, status)`

## 9. API Design

```
GET    /api/v1/appointments                # filters: patient_id, doctor_id, date, status, type
POST   /api/v1/appointments                # Idempotency-Key required
GET    /api/v1/appointments/{id}
PATCH  /api/v1/appointments/{id}           # reschedule only
POST   /api/v1/appointments/{id}/check-in
POST   /api/v1/appointments/{id}/start
POST   /api/v1/appointments/{id}/complete
POST   /api/v1/appointments/{id}/cancel
POST   /api/v1/appointments/{id}/no-show
GET    /api/v1/appointments/{id}/status-history
POST   /api/v1/appointments/recommend-slot   # optional AI suggestion of one slot; books nothing
GET    /api/v1/appointments/queue?doctor_id  # walk-in queue view
```

**Request example — POST /appointments:**

```json
{
  "patient_id": "...",
  "doctor_id": "...",
  "scheduled_start": "2026-08-15T09:15:00+05:30",
  "scheduled_end": "2026-08-15T09:30:00+05:30",
  "type": "new",
  "reason": "Persistent cough for 5 days",
  "notes": "Prefers morning slots"
}
```

The gate the booking dialog reads before offering the suggestion lives with Hospital Settings:

```
GET    /api/v1/hospitals/current/feature-flags   # any authenticated user; one `available` boolean per feature
```

**Request example — POST /appointments/recommend-slot:**

```json
{
  "patient_id": "...",
  "doctor_id": "...",
  "date": "2026-08-15"
}
```

**Response `data` — a suggestion:**

```json
{
  "status": "recommended",
  "recommendation": {
    "slot_start": "2026-08-15T09:15:00+05:30",
    "slot_end": "2026-08-15T09:30:00+05:30",
    "doctor_id": "...",
    "reason": "..."
  },
  "date": "2026-08-15",
  "timezone": "Asia/Kolkata",
  "candidate_count": 4
}
```

When the day has no free slot left, `status` is `no_free_slots`, `recommendation` is `null` and `candidate_count` is `0`.

## 10. Permissions

- `appointment.read`
- `appointment.read.own` (doctor's own schedule)
- `appointment.book`
- `appointment.reschedule`
- `appointment.cancel`
- `appointment.check_in`
- `appointment.start`
- `appointment.complete`
- `appointment.book_override` (bypass availability constraint)
- `appointment.recommend_slot` — the recommend-slot endpoint also requires `doctor.availability.read` (Doctor Management). Of the seeded hospital roles, Hospital Admin and Receptionist hold both.

## 11. Validation Rules

- `scheduled_start` in the future (or within a 15-minute grace for walk-in).
- Duration in {10, 15, 20, 30, 45, 60} minutes.
- Type ∈ {new, follow_up, walk_in, emergency}.
- Reason ≤ 500 chars.
- Cancellation reason required.
- Slot recommendation: `patient_id`, `doctor_id` and `date` are all required; `date` is `YYYY-MM-DD` between 2000-01-01 and 2100-12-31; any other field is rejected. The patient and the doctor must belong to the caller's hospital.

## 12. UI Requirements

- Reception dashboard: today's schedule as a timeline; walk-in queue side panel; drag to reschedule (v2.1).
- Booking modal: patient search → doctor pick → date/slot → confirm.
- Doctor calendar: day / week views with color-coded status.
- AI slot suggestion inside the booking dialog (not a drawer, not a ranked list): an optional button, shown only when the feature is available to the user; one suggested time with the model's reason, labelled as AI-suggested; "Use this slot" selects it in the picker and the Book button confirms. The reason is untrusted model text, rendered as plain text. If the suggestion fails, a message says so and the picker stays usable.
- Status pill with clear color coding across the app.

## 13. AI Integration Points

As built:

- **Prompt:** `appointment.recommend_slot` (versioned template in `backend/app/ai/prompts/templates/appointment/recommend_slot.yaml`)
- **Provider hint:** `fast`
- **Optional:** needs the hospital flag (Section 18) and AI configured on the server. With either missing, the module works unchanged and the endpoint answers `FEATURE_DISABLED` or `AI_NOT_CONFIGURED`.
- **Data scope:** one doctor's slots for one hospital-local day, as clock times — free slots under opaque per-request ids, the others marked unavailable — plus the date, its weekday and the number of free slots. **No patient data** (no identity, history, preferences or appointment reason), and no doctor, hospital or user identifiers. The model is therefore availability-aware, not patient-aware.
- **Tools available:** none. The server computes the candidate slots and puts them in the prompt; the model cannot query anything.
- **Output shape:** one JSON object `{ "slot_id": "<an offered id>", "reason": "<one sentence>" }`. No score, no list.
- **Server-side validation:** the reply is parsed strictly and must name an id that was offered; the chosen slot is then re-checked against the database. A reply that is truncated, not JSON, the wrong shape or names anything else is rejected with `AI_RESPONSE_INVALID`. The slot times returned to the client are the server's own; only `reason` is model-written, and it is cleaned, capped at 200 characters and treated as untrusted text.
- **Failure behaviour:** one model call per request, with a deadline; no retry and no fallback provider. Failures are typed errors, never an empty success.
- **Safety:** the model can only recommend; the reception clicks book. The prompt instructs it to make no clinical judgement, and it is given nothing about urgency. A recommendation writes nothing — no appointment and no audit entry; the booking that follows is audited as the staff member's.

Not built — in the original spec for this section, now future scope:

- Data scope of doctor load over the next 7 days, the patient's past appointment cadence, and urgency.
- Tools `list_doctor_slots(date_range)` and `list_appointments_by_doctor(date_range)`.
- A function-call output of several slots `[{slot_start, slot_end, score, reason}]`.

Future (v2.1):
- `appointment.reminder_text` — draft the reminder message to the patient in their preferred language.

## 14. Edge Cases

- Simultaneous booking of the same slot from two receptionists → one wins on DB constraint; the loser gets 409 with a fresh slot fetch suggestion.
- Doctor deletes availability with existing bookings → doctors module blocks that; users must reassign appointments first.
- Booking at 23:59 with a duration crossing midnight → allowed; timezone-aware.
- Reschedule to a slot that's now unavailable → 409.
- Complete without check-in? Allowed for doctor discretion, but audit records skipped states.
- Cancellation window (e.g. no cancel within 1 hour) is v2.1; MVP allows any time before start.

## 15. Cross-Module Dependencies

- Depends on: Patient (patient reference), Doctor (availability, slot generation), Notification (confirmations, reminders), Audit, AI service (optional — slot suggestion only), Hospital Settings (the `feature.ai.slot_recommendation` flag).
- Provides to: Consultation (an appointment is the container for a visit), Billing (invoice drafted on complete).

## 16. Testing Requirements

- Unit: state machine transitions, idempotency, no-overlap logic.
- Repository: overlap constraint, state history queries.
- API: all endpoints × status × permission combinations.
- Integration: full booking → complete → invoice draft.
- Load: 100 concurrent bookings; overlap constraint holds.

## 17. Acceptance Criteria

- AC-1: A receptionist can book an appointment in under 30 seconds.
- AC-2: Double-booking is prevented at the database level.
- AC-3: The status of an appointment can only follow the defined state machine.
- AC-4: Where the feature is enabled and AI is configured, a permitted user can ask for an AI slot suggestion and receives exactly one slot that the server itself computed as free and re-checked, or an explicit "no free slots" or error outcome. Nothing is booked until the user confirms through the normal booking. With the feature off or AI unavailable, booking by hand is unaffected. _(Originally: "returns 3 ranked options in under 2 seconds" — a ranked list was not built, and the 2-second figure is an unmeasured target; see Section 7.)_
- AC-5: A no-show sweeper marks appointments correctly within 10 minutes of the grace window.
- AC-6: Every state change writes a history row.

## 18. Rollout Plan

- Ships with MVP.
- AI slot recommendation behind `feature.ai.slot_recommendation` flag — a per-hospital key in `hospitals.settings`, on only when stored as exactly `true`.
  - The flag alone is not enough: the server must also have AI configured. `GET /hospitals/current/feature-flags` reports the two combined as one `available` boolean.
  - There is no endpoint to switch the flag: `PATCH /hospitals/current` refuses `feature.*` keys, and the platform toggle is not built, so today it is set in the database. The demo seed turns it on for the demo hospital unless it was already set explicitly.

## 19. Future Scope

- AI slot recommendation beyond the built version: urgency and preferred-window inputs, suggestions across doctors, use of doctor load and of the patient's visit history or preferences, several ranked options
- AI-generated reminder text (v2.1)
- Recurring appointments (v2.1)
- Wait-list & auto-fill on cancellations (v2.1)
- Drag-to-reschedule on the calendar UI (v2.1)
- Video consultation booking (v3)
- Multi-doctor visits (team-based) (v2.2)

## 20. Open Questions

- Grace period for no-show — is 30 minutes reasonable across all pilot hospitals? Confirm with pilot 1 during onboarding.
