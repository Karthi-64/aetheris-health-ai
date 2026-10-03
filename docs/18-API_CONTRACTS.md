# 18 — Frontend API Contract Reference

The exact request/response contracts for the endpoints the frontend consumes today:
**Patients, Departments, Doctors, Appointments, Billing**. Written so a frontend module
can be built without reading backend code or guessing a shape.

**Source of truth.** Every contract below was read out of the implementation, not the
design docs — routers in `backend/app/api/v1/`, DTOs in `backend/app/schemas/`, and the
assertions in `backend/app/tests/`. Where a design doc and the code disagreed, the code
won and the disagreement is called out. Conventions come from
[06-API_STANDARDS.md](06-API_STANDARDS.md); this document does not restate them, it
records what is actually wired.

Anything not listed here is not available yet. Do not build against it.

---

## 1. Conventions

### 1.1 Base URL

```
/api/v1
```

The frontend Axios instance already defaults to `/api/v1` (`frontend/src/lib/api.ts`);
in dev the Vite proxy forwards `/api` to the backend.

### 1.2 Response envelope

Every response — success or failure — is wrapped. Single resource:

```json
{
  "success": true,
  "message": "Patient retrieved.",
  "data": { "...": "..." },
  "metadata": null
}
```

> **`metadata` is `null` on single-resource successes.** [06-API_STANDARDS.md](06-API_STANDARDS.md)
> §5.1 shows `metadata.request_id` on every response, but only the error handlers
> and the list routers actually build a metadata block. On a success the
> correlation id comes back in the **`X-Request-ID` response header** instead, and
> list responses carry `metadata.pagination` with `request_id` still null. Do not
> dereference `metadata.request_id` on a success path. This is a doc-vs-code
> divergence, pinned by a test (`test_metadata_is_null_on_a_single_resource_success`)
> and raised with the team — until it is settled, the behaviour above is what ships.

List:

```json
{
  "success": true,
  "message": "Patients retrieved.",
  "data": [ { "...": "..." } ],
  "metadata": {
    "request_id": null,
    "pagination": { "page": 1, "page_size": 25, "total_records": 137, "total_pages": 6 }
  }
}
```

Failure — the fields are **flat**, there is no nested `error` object:

```json
{
  "success": false,
  "message": "Validation failed.",
  "errors": [ { "field": "date_of_birth", "message": "Cannot be in the future" } ],
  "error_code": "VALIDATION_ERROR",
  "metadata": { "request_id": "b6e2..." }
}
```

`frontend/src/api/http.ts` already unwraps `data` and maps `metadata.pagination` into
camelCase. Keep using it — components should never see the envelope.

### 1.3 Error codes

From `backend/app/core/error_codes.py`:

| `error_code` | HTTP | When you will hit it |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Pydantic rejected the body or a query param |
| `AUTHENTICATION_REQUIRED` | 401 | Missing/expired access token |
| `PERMISSION_DENIED` | 403 | Authenticated, but the role lacks the permission |
| `RESOURCE_NOT_FOUND` | 404 | Wrong id, or the row belongs to another hospital |
| `RESOURCE_CONFLICT` | 409 | Duplicate MRN/code/licence, or a double-booked slot |
| `BUSINESS_RULE_VIOLATION` | 400 | Illegal state transition, already-deactivated record |
| `RATE_LIMITED` | 429 | 300 req/min per user, 1000/min per hospital |

A 404 is deliberately returned for a record in another tenant. Do not treat it as a bug.

### 1.4 Authentication

`Authorization: Bearer <access_token>` on **every** endpoint in this document. There are
no public patient/doctor/appointment endpoints.

**Both tokens travel in JSON bodies — there is no cookie.** The backend never sets one.

- `POST /api/v1/auth/login` returns `{ access_token, refresh_token, expires_in, user }`.
- `POST /api/v1/auth/refresh` takes `{ "refresh_token": "…" }` and returns
  `{ access_token, refresh_token, expires_in }` — no `user`.
- The access token lives 15 minutes (`expires_in: 900`); the refresh token 7 days.

Keep both **in memory only** (`frontend/src/services/tokenStore.ts`), never in
`localStorage` or `sessionStorage`. A page reload therefore drops the session and lands
on `/login` — that is expected, not a bug.

**Refresh tokens rotate, and reuse is treated as theft.** Every successful refresh
returns a *new* refresh token and revokes the old one. Presenting an already-used refresh
token revokes **all of that user's sessions** and returns `401` ("All sessions
invalidated"). The practical consequence: never run two refreshes at once with the same
token. `frontend/src/lib/api.ts` already guards this — concurrent `401`s share a single
in-flight refresh, the original request is retried once with the new token, and if the
refresh itself fails the session is cleared and route guards send the user to `/login`.
Keep that shape if you touch it, and give any second client the same single-flight guard.

> **Diverges from `CLAUDE.md`,** which specifies the refresh token as an HTTP-only cookie.
> That is the intended design; what ships is the body-based flow above. See §9.

### 1.5 Tenancy

`hospital_id` is taken from the authenticated user's token. It is never a query param and
never accepted in a body. A Super Admin (no hospital) gets `400 BUSINESS_RULE_VIOLATION`
on these endpoints — that is intentional, not a bug: cross-tenant reads are a separate
audited capability. Log in as a hospital-scoped user.

### 1.6 Pagination

`?page=1&page_size=25`. `page` ≥ 1, `page_size` 1–100 (default 25). Out-of-range values
are a `422`, not a clamp. Counts come back in `metadata.pagination`.

### 1.7 Time

All datetimes on the wire are ISO 8601 **with an offset**, stored UTC. A naive datetime
is rejected. `date` fields (`date_of_birth`, `?date=`) are plain `YYYY-MM-DD`.

Doctor availability is the one exception: it is *wall-clock* in the hospital's timezone
(`Asia/Kolkata` for the demo hospital), because a clinic that opens at 09:00 opens at
09:00 either side of a DST change.

### 1.8 Sorting

**No endpoint in this document accepts a `sort` parameter.** Order is fixed per resource
and documented below. `06-API_STANDARDS.md` §11 describes a `?sort=` convention — it is
not implemented for these resources. Sort client-side within a page, or ask before
building UI that needs server-side sort.

### 1.9 Roles → permissions

Which seeded role can call what. Relevant subset only:

| Permission | Hospital Admin | Doctor | Nurse | Receptionist |
|---|:--:|:--:|:--:|:--:|
| `patient.read` | ✅ | ✅ | ✅ | ✅ |
| `patient.create` | ✅ | ✅ | — | ✅ |
| `patient.update` | ✅ | ✅ | ✅ | — |
| `patient.delete` | ✅ | — | — | — |
| `department.read` | ✅ | ✅ | ✅ | ✅ |
| `department.create/update/delete` | ✅ | — | — | — |
| `doctor.read` | ✅ | ✅ | ✅ | ✅ |
| `doctor.create/update/delete` | ✅ | — | — | — |
| `doctor.availability.read` | ✅ | ✅ | ✅ | ✅ |
| `doctor.availability.update` | ✅ | ✅ | — | — |
| `doctor.leave.create/delete` | ✅ | ✅ | — | — |
| `appointment.read` | ✅ | ✅ | ✅ | ✅ |
| `appointment.book` | ✅ | — | — | ✅ |
| `appointment.reschedule` | ✅ | — | — | ✅ |
| `appointment.cancel` | ✅ | — | — | ✅ |
| `appointment.check_in` | ✅ | ✅ | ✅ | ✅ |
| `appointment.start` / `.complete` | ✅ | ✅ | — | — |
| `appointment.recommend_slot` | ✅ | — | — | ✅ |
| `appointment.book_override` | ✅ | — | — | — |

Two gotchas worth designing around: a **Receptionist cannot edit or deactivate a patient**,
and a **Doctor cannot book or cancel** — hide those controls per permission rather than
letting the call 403.

---

## 2. Patients

`backend/app/api/v1/patients.py` · `backend/app/schemas/patient.py`

### 2.1 Endpoints

| Method | Path | Permission | Success |
|---|---|---|---|
| POST | `/api/v1/patients` | `patient.create` | 201 |
| GET | `/api/v1/patients` | `patient.read` | 200 (paginated) |
| GET | `/api/v1/patients/{patient_id}` | `patient.read` | 200 |
| PATCH | `/api/v1/patients/{patient_id}` | `patient.update` | 200 |
| DELETE | `/api/v1/patients/{patient_id}` | `patient.delete` | 200 (soft delete) |

`DELETE` returns the updated record with `status: "inactive"`, not a 204 — the row is
never removed, because appointments and invoices keep referencing it.

### 2.2 Canonical field shapes

- **Name** is stored as `first_name` + `last_name`. Both responses also carry a computed
  `full_name`. There is no single `name` input field — a create must supply both parts.
- **Age** is never stored or accepted. `date_of_birth` is the input; `age` is computed at
  response time and is present on both response shapes. Do not send `age`.
- **Gender** is `"male" | "female" | "other" | "unspecified"` — lowercase, and fixed by
  the `gender_enum` Postgres type ([05-DATABASE_DESIGN.md](05-DATABASE_DESIGN.md) §2.7).
  `"M"`/`"F"` are rejected with 422. `unspecified` exists so "declined to answer" is
  recorded as itself rather than being coerced to `other`.
- **MRN** is generated server-side, unique per hospital, format `MRN-{year}-{seq:05d}`
  (e.g. `MRN-2026-00042`). It cannot be supplied on create and cannot be changed on update.
- **`status`** is `"active" | "inactive"` and is derived from the soft-delete column. It
  is **not** a clinical state — there is no Admitted/Discharged/Critical concept in this
  module, and no admissions module exists yet.
- A patient has **no assigned doctor or department**. That relationship lives on
  appointments. To show "current doctor" in a patient list you would have to join
  appointments client-side; nothing on the patient endpoint provides it.

### 2.3 `POST /api/v1/patients`

Request (`CreatePatientRequest`) — required: `first_name`, `last_name`, `date_of_birth`,
`gender`. Everything else optional.

```json
{
  "first_name": "Ananya",
  "last_name": "Rao",
  "date_of_birth": "1988-03-14",
  "gender": "female",
  "phone": "+919812345678",
  "email": "ananya@example.com",
  "blood_group": "B+",
  "address": {
    "line1": "12, MG Road",
    "city": "Hyderabad",
    "state": "TS",
    "postal_code": "500001",
    "country": "IN"
  }
}
```

Validation the form must respect:

| Field | Rule |
|---|---|
| `first_name`, `last_name` | 1–100 chars |
| `date_of_birth` | not in the future; age ≤ 130 |
| `gender` | the four enum values above |
| `phone` | normalized to E.164, max 20 chars; blank is allowed, malformed is 422 |
| `email` | lowercased and trimmed; must have one `@` and a dotted domain |
| `address.country` | uppercased ISO country code |
| `emergency_contact` | `{ name, phone, relation }` — phone must be E.164 |
| `allergies[]` | `{ name, severity: "mild"\|"moderate"\|"severe", reaction?, noted_on? }` |
| `chronic_conditions[]` | `{ name, since_year?, notes? }` |
| `current_medications[]` | `{ name, dosage?, frequency?, started_on? }` |

Response `201` → `PatientResponse` (§2.6). `409 RESOURCE_CONFLICT` if no MRN could be
allocated.

### 2.4 `GET /api/v1/patients`

| Query param | Type | Notes |
|---|---|---|
| `q` | string ≤100 | **Prefix** match on first *or* last name (case-insensitive), **exact** match on MRN or phone. Not a substring search — `"ao"` will not find `"Rao"`. |
| `gender` | enum | Exact |
| `date_of_birth` | `YYYY-MM-DD` | Exact |
| `age_gte` / `age_lte` | int 0–130 | Inverted range is 422 |
| `include_inactive` | bool | Default `false` |
| `page` | int ≥1 | Default 1 |
| `page_size` | int 1–100 | Default 25 |

Unknown query params are ignored by FastAPI, but sending `search=` or `pageSize=` simply
does nothing — the names are `q` and `page_size`.

Order is fixed: `last_name`, then `first_name`, then `id`. The `id` tiebreak makes paging
stable.

Response `data[]` is `PatientSummaryResponse` — deliberately lighter than the detail
shape, with no medical history:

```json
{
  "id": "3f6c1b2e-...",
  "mrn": "MRN-2026-00042",
  "first_name": "Ananya",
  "last_name": "Rao",
  "full_name": "Ananya Rao",
  "date_of_birth": "1988-03-14",
  "age": 38,
  "gender": "female",
  "phone": "+919812345678",
  "status": "active"
}
```

### 2.5 `PATCH /api/v1/patients/{id}`

Partial update. Omitted fields are untouched; sending explicit `null` for a `NOT NULL`
column (`first_name`, `last_name`, `date_of_birth`, `gender`) is rejected. `mrn` and
`hospital_id` are rejected outright.

### 2.6 `PatientResponse` (create / get / update)

`PatientSummaryResponse` plus: `hospital_id`, `blood_group`, `email`, `address`,
`emergency_contact`, `marital_status`, `occupation`, `allergies[]`,
`chronic_conditions[]`, `current_medications[]`, `notes`, `created_at`, `updated_at`.
`full_name` and `age` are computed here too.

---

## 3. Departments

`backend/app/api/v1/departments.py` · `backend/app/schemas/department.py`

| Method | Path | Permission | Success |
|---|---|---|---|
| POST | `/api/v1/departments` | `department.create` | 201 |
| GET | `/api/v1/departments` | `department.read` | 200 (paginated) |
| GET | `/api/v1/departments/{id}` | `department.read` | 200 |
| PATCH | `/api/v1/departments/{id}` | `department.update` | 200 |
| DELETE | `/api/v1/departments/{id}` | `department.delete` | 200 (soft delete) |
| POST | `/api/v1/departments/{id}/activate` | `department.update` | 200 |

Query params on the list: `q`, `include_inactive`, `page`, `page_size`. `q` is a
case-insensitive **prefix** match on name and an **exact** match on code.

`DepartmentSummaryResponse` (list rows):

```json
{ "id": "…", "code": "CARD", "name": "Cardiology", "location": "Block A, 2nd floor", "status": "active" }
```

`DepartmentResponse` (detail) adds `hospital_id`, `description`, `phone_extension`,
`email`, `created_at`, `updated_at`.

`code` is unique per hospital and is what a duplicate 409 refers to.

---

## 4. Doctors

`backend/app/api/v1/doctors.py` · `backend/app/schemas/doctor.py`

### 4.1 Endpoints

| Method | Path | Permission |
|---|---|---|
| POST | `/api/v1/doctors` | `doctor.create` |
| GET | `/api/v1/doctors` | `doctor.read` |
| GET | `/api/v1/doctors/{id}` | `doctor.read` |
| PATCH | `/api/v1/doctors/{id}` | `doctor.update` |
| DELETE | `/api/v1/doctors/{id}` | `doctor.delete` |
| POST | `/api/v1/doctors/{id}/activate` | `doctor.update` |
| GET | `/api/v1/doctors/{id}/availability` | `doctor.availability.read` |
| PUT | `/api/v1/doctors/{id}/availability` | `doctor.availability.update` |
| GET | `/api/v1/doctors/{id}/leaves` | `doctor.read` |
| POST | `/api/v1/doctors/{id}/leaves` | `doctor.leave.create` |
| DELETE | `/api/v1/doctors/{id}/leaves/{leave_id}` | `doctor.leave.delete` |
| GET | `/api/v1/doctors/{id}/slots?date=YYYY-MM-DD` | `doctor.availability.read` |

### 4.2 Doctor ↔ user ↔ department

A doctor is a **profile attached to an existing user**, not a standalone person. `POST
/doctors` takes a `user_id` — the user must already exist. There is no "create doctor and
user in one call" endpoint. Name and email come from that user record, which is why
`full_name` is read-only on the doctor and there are no name fields on create/update.

`department_id` is **nullable** — a doctor may be unassigned. `department_name` is
denormalized into both response shapes so a list view needs no second call.

### 4.3 `GET /api/v1/doctors`

| Query param | Notes |
|---|---|
| `q` | Prefix match on the user's first/last name, exact match on `license_number` |
| `specialization` | **Exact** string match, not a prefix — send the value verbatim |
| `department` | Department **UUID** (note: the param is `department`, not `department_id`) |
| `include_inactive` | Default `false` |
| `page`, `page_size` | As §1.6 |

`DoctorSummaryResponse`:

```json
{
  "id": "…",
  "user_id": "…",
  "full_name": "Priya Sharma",
  "specialization": "Cardiology",
  "department_id": "…",
  "department_name": "Cardiology",
  "consultation_fee": "800.00",
  "status": "active"
}
```

`consultation_fee` is a **string-serialized decimal**, not a JSON number — money is
`NUMERIC(15,2)`. Parse it as a decimal string; do not run it through `parseFloat` for
anything but display.

`DoctorResponse` (detail) adds `hospital_id`, `email`, `license_number`,
`qualifications[]` (`{ degree, institution?, year? }`), `languages[]`, `bio`,
`created_at`, `updated_at`.

### 4.4 Availability

`GET` returns every window ordered by day then start time:

```json
{ "id": "…", "day_of_week": 0, "start_time": "09:00:00", "end_time": "13:00:00", "slot_duration_minutes": 30 }
```

`day_of_week` is **0 = Monday … 6 = Sunday**. Not the JS `Date.getDay()` convention —
convert.

`PUT` is a **full replace**, not a merge: `{ "entries": [...] }` becomes the entire weekly
schedule and omitted days are cleared. An empty `entries` list wipes availability.
Same-day windows must not overlap; windows that touch exactly (one ends when the next
starts) are legal and are how a mid-day slot-length change is expressed.

### 4.5 Leaves

`POST /doctors/{id}/leaves` takes `{ starts_at, ends_at, reason? }` — timezone-aware ISO
8601, `ends_at` exclusive. `LeaveResponse` echoes those plus `id` and `doctor_id`, in UTC.

### 4.6 Slots

`GET /doctors/{id}/slots?date=YYYY-MM-DD` — the parameter is `date`.

Slots are a **read model**: computed on demand from availability, leaves and existing
appointments. They are never stored, so there is no "slot id" to book against; you book by
sending `scheduled_start`/`scheduled_end` to `POST /appointments`.

```json
{
  "date": "2026-08-24",
  "doctor_id": "…",
  "timezone": "Asia/Kolkata",
  "slots": [
    { "start": "2026-08-24T09:00:00+05:30", "end": "2026-08-24T09:30:00+05:30", "status": "available", "appointment_id": null },
    { "start": "2026-08-24T09:30:00+05:30", "end": "2026-08-24T10:00:00+05:30", "status": "booked", "appointment_id": "…" }
  ]
}
```

`status` is `available | booked | on_leave`. Times are in the hospital's timezone, echoed
in `timezone`.

---

## 5. Appointments

`backend/app/api/v1/appointments.py` · `backend/app/schemas/appointment.py`

### 5.1 Endpoints

| Method | Path | Permission | Notes |
|---|---|---|---|
| POST | `/api/v1/appointments` | `appointment.book` | **Requires `Idempotency-Key` header** |
| GET | `/api/v1/appointments` | `appointment.read` | Paginated |
| GET | `/api/v1/appointments/queue` | `appointment.read` | Walk-in queue, unpaginated list |
| GET | `/api/v1/appointments/{id}` | `appointment.read` | |
| PATCH | `/api/v1/appointments/{id}` | `appointment.reschedule` | Reschedule only |
| POST | `/api/v1/appointments/{id}/check-in` | `appointment.check_in` | |
| POST | `/api/v1/appointments/{id}/start` | `appointment.start` | |
| POST | `/api/v1/appointments/{id}/complete` | `appointment.complete` | |
| POST | `/api/v1/appointments/{id}/cancel` | `appointment.cancel` | Body: `{ "reason": "…" }` |
| POST | `/api/v1/appointments/{id}/no-show` | `appointment.cancel` | |
| GET | `/api/v1/appointments/{id}/status-history` | `appointment.read` | |
| POST | `/api/v1/appointments/recommend-slot` | `appointment.recommend_slot` | AI ranking |

### 5.2 Booking

```
POST /api/v1/appointments
Idempotency-Key: 9f1c4b2a-...
```

```json
{
  "patient_id": "…",
  "doctor_id": "…",
  "scheduled_start": "2026-08-24T09:30:00+05:30",
  "scheduled_end": "2026-08-24T10:00:00+05:30",
  "type": "new",
  "reason": "Chest pain follow-up",
  "notes": "Patient prefers morning"
}
```

Four things the client must get right:

1. **`Idempotency-Key` is required**, 8–200 chars. Generate a UUID per booking attempt and
   reuse it across retries of that same attempt. A replay returns **200** with the original
   appointment and the message "Appointment already booked with this key." — not 201, and
   not a second booking. Treat 200 and 201 as the same success path in the UI.
2. There is **no `department_id`** on an appointment. Department is reached through the
   doctor. A department filter in the UI means: resolve doctors in that department first,
   then filter/query by `doctor_id`.
3. **Double-booking is caught by a database exclusion constraint**, so two receptionists
   racing the same slot means one gets `409 RESOURCE_CONFLICT` with the conflicting
   appointment attached. Handle 409 as "slot just went", refresh slots, do not retry blind.
4. Booking **outside published availability** needs `appointment.book_override`, which only
   Hospital Admin holds. Without it, an out-of-hours slot is a 400.

### 5.3 `GET /api/v1/appointments`

| Query param | Notes |
|---|---|
| `patient_id`, `doctor_id` | UUID |
| `date` | Single calendar day, `YYYY-MM-DD`, **in the hospital's own timezone** |
| `status` | `booked \| checked_in \| in_progress \| completed \| cancelled \| no_show` |
| `type` | `new \| follow_up \| walk_in \| emergency` |
| `page`, `page_size` | As §1.6 |

`date` means the clinic's calendar day: local midnight to the next local midnight in the
hospital's configured timezone (`Asia/Kolkata` for the demo hospital). Send the local date
and nothing else. There is **no `tz_offset_hours` parameter any more** — it could only
express whole hours, which is wrong for India (UTC+5:30), so the server now resolves the
day itself. A client that still sends it is not refused; the value is ignored.

The names are `date`, `status` and `type` — not `appointment_date`, `appointment_status`
or `appointment_type`. Unknown query parameters are ignored, so a wrong name does not fail:
it returns every appointment, unfiltered.

Order is earliest-first by `scheduled_start`.

`AppointmentSummaryResponse`:

```json
{
  "id": "…",
  "patient_id": "…",
  "patient_name": "Ananya Rao",
  "doctor_id": "…",
  "doctor_name": "Priya Sharma",
  "scheduled_start": "2026-08-24T04:00:00Z",
  "scheduled_end": "2026-08-24T04:30:00Z",
  "status": "booked",
  "type": "new"
}
```

`patient_name` and `doctor_name` are denormalized in — a list view needs no extra lookups.

`AppointmentResponse` (detail) adds `hospital_id`, `reason`, `notes`, `cancelled_reason`,
`checked_in_at`, `started_at`, `completed_at`, `created_at`, `updated_at`.

### 5.4 Status lifecycle

```
booked ──check-in──> checked_in ──start──> in_progress ──complete──> completed
   │                      │                     │
   └──cancel/no-show──────┴─────────────────────┘
```

`completed`, `cancelled` and `no_show` are **terminal** — a cancelled appointment is never
reactivated, a new one is booked instead. An illegal transition is `400
BUSINESS_RULE_VIOLATION`, so drive the action buttons off the current `status` rather than
letting the call fail.

`GET /{id}/status-history` returns the audit trail: `{ id, from_status, to_status,
changed_by, changed_at, reason }`, append-only.

### 5.5 Walk-in queue

`GET /api/v1/appointments/queue?doctor_id=…` returns unfinished walk-ins in arrival order
(check-in time where the patient has arrived, booking time otherwise), as a plain list —
**no pagination metadata**, so use `http.get`, not `http.getPaginated`.

There is **no token/queue-number field** on an appointment. Queue position is the array
index in this response. Do not display a "token number" as if the backend issued one.

### 5.6 AI slot recommendation

`POST /api/v1/appointments/recommend-slot` with `{ patient_id, doctor_id?, urgency?,
preferred_window_start?, preferred_window_end?, limit? }` returns
`{ recommendations: [{ slot_start, slot_end, doctor_id, score, reason }], model }`.
`score` is 0–1. `limit` is 1–10, default 3.

---

## 6. Billing

`backend/app/api/v1/services.py` · `backend/app/api/v1/invoices.py` ·
`backend/app/schemas/billing.py`

The core money path of [modules/06-billing.md](modules/06-billing.md): a services
catalog, draft invoices, issue, payments, void. **Discounts, refunds, PDF and AI explain
are not built** — see §6.10.

### 6.1 Endpoints

| Method | Path | Permission | Success |
|---|---|---|---|
| GET | `/api/v1/services` | `service.read` | 200 (paginated) |
| POST | `/api/v1/services` | `service.create` | 201 |
| GET | `/api/v1/services/{service_id}` | `service.read` | 200 |
| PATCH | `/api/v1/services/{service_id}` | `service.update` | 200 |
| GET | `/api/v1/invoices` | `invoice.read` or `invoice.read.own` | 200 (paginated) |
| POST | `/api/v1/invoices` | `invoice.create` | 201 |
| GET | `/api/v1/invoices/{invoice_id}` | `invoice.read` or `invoice.read.own` | 200 |
| PATCH | `/api/v1/invoices/{invoice_id}` | `invoice.update` | 200 — drafts only |
| POST | `/api/v1/invoices/{invoice_id}/issue` | `invoice.issue` | 200 |
| POST | `/api/v1/invoices/{invoice_id}/void` | `invoice.void` | 200 |
| POST | `/api/v1/invoices/{invoice_id}/payments` | `invoice.payment.record` or `invoice.payment.record.cash` | 201, or 200 on a replay. **Requires `Idempotency-Key`** |
| GET | `/api/v1/invoices/{invoice_id}/payments` | `invoice.read` or `invoice.read.own` | 200 (plain list) |

Two of these codes are **narrow** versions of a wider one, and the server applies the
limit — see §6.9. A user holding both the wide and the narrow code is not limited.

### 6.2 Money

- **Every amount is a decimal string** — `"1200.00"`, never a JSON number. Send them as
  strings too. Do not run them through `parseFloat` for anything but display.
- **The server computes every total.** No request body has `total`, `subtotal`,
  `tax_amount`, `line_total` or `tax_rate`; sending one is a `422`. To preview a total
  while editing, compute it for display only and trust the response.
- Per line: `net = unit_price × quantity`, `tax = net × tax_rate ÷ 100`, each rounded
  **half-to-even** to two places; `line_total = net + tax`. The invoice `subtotal` is the
  sum of the nets, `tax_amount` the sum of the taxes, and
  `total = subtotal + tax_amount − discount_amount`.
- `tax_rate` is a percentage (`"18.00"` = 18%). It is the hospital's configured rate for
  a taxable line and `"0.00"` otherwise. The demo hospital configures none, so every
  seeded line is untaxed.
- `currency` (ISO 4217, `"INR"` for the demo hospital) is on every invoice shape. It is
  the hospital's and cannot be set per invoice.
- `discount_amount` is always `"0.00"` today — discounts are not built.

### 6.3 Services catalog

`ServiceResponse` (list rows and detail are the same shape):

```json
{
  "id": "…",
  "code": "ECG",
  "name": "ECG (12-lead)",
  "category": "Diagnostics",
  "price": "450.00",
  "taxable": false,
  "is_active": true,
  "created_at": "2026-10-01T14:17:10Z",
  "updated_at": "2026-10-01T14:17:10Z"
}
```

`GET /services` query params:

| Query param | Notes |
|---|---|
| `q` | **Prefix** match on name (case-insensitive), **exact** match on code |
| `category` | Exact string match |
| `is_active` | `true` / `false`. Omit for both — inactive services are **included** by default, unlike the other lists |
| `page`, `page_size` | As §1.6 |

Order is fixed: `name`, then `id`.

`POST /services` — required: `code`, `name`, `price`. Optional: `category`, `taxable`
(default `true`).

| Field | Rule |
|---|---|
| `code` | 1–50 chars, letters/digits/`-`/`_`, uppercased automatically, unique per hospital (duplicate → `409`) |
| `name` | 1–200 chars, not blank |
| `price` | ≥ 0, at most 2 decimal places |

`PATCH /services/{id}` takes any of `name`, `category`, `price`, `taxable`, `is_active`.
**`code` cannot be changed** (`422`). There is no delete: retire a service with
`"is_active": false`. Changing `price` never alters an existing invoice — lines keep the
price they were written with.

### 6.4 Invoices

```
draft ──issue──> issued ──payment──> partially_paid ──payment──> paid
                   │   └──────────────payment in full──────────────┘
                   └──void──> void
```

`status` is `draft | issued | partially_paid | paid | void | refunded`. Nothing produces
`refunded` yet. `paid` and `void` are terminal. Drive the action buttons off `status`:

| Status | Edit | Issue | Record payment | Void |
|---|:--:|:--:|:--:|:--:|
| `draft` | ✅ | ✅ | — | — |
| `issued` | — | — | ✅ | ✅ |
| `partially_paid` | — | — | ✅ | — |
| `paid`, `void` | — | — | — | — |

A disallowed action is `400 BUSINESS_RULE_VIOLATION`.

**`POST /invoices`** creates a **draft**. Required: `patient_id`. Optional:
`appointment_id`, `items[]` (max 200, may be empty), `notes` (≤ 2000).

```json
{
  "patient_id": "…",
  "appointment_id": "…",
  "items": [
    { "service_id": "…", "quantity": "1" },
    { "description": "Crepe bandage", "quantity": "2", "unit_price": "75.00" }
  ],
  "notes": "Counter items"
}
```

A line is one of two shapes, and nothing in between:

| | Catalog line | Ad-hoc line |
|---|---|---|
| `service_id` | required | omit |
| `description` | optional — overrides the service name on this invoice | **required** |
| `unit_price` | **must be omitted** — comes from the catalog | **required**, ≥ 0 |
| `taxable` | **must be omitted** — comes from the catalog | optional, default `false` |
| `quantity` | optional, default `"1"`, > 0, ≤ 2 decimals | same |

An inactive service, or one from another hospital, is a `422` naming the line
(`items.1.service_id`). An `appointment_id` must belong to the same patient (`422`
otherwise), and **an appointment can have only one live invoice** — a second is
`409 RESOURCE_CONFLICT`. Void invoices do not count, so void-and-re-raise works.

**`InvoiceResponse`** (create / get / patch / issue / void):

```json
{
  "id": "…",
  "hospital_id": "…",
  "invoice_number": "INV-2026-000003",
  "patient_id": "…",
  "patient_name": "Thomas George",
  "appointment_id": "…",
  "status": "partially_paid",
  "currency": "INR",
  "items": [
    {
      "id": "…",
      "service_id": "…",
      "description": "Follow-up consultation",
      "quantity": "1.00",
      "unit_price": "300.00",
      "tax_rate": "0.00",
      "line_total": "300.00",
      "position": 0
    },
    {
      "id": "…",
      "service_id": "…",
      "description": "ECG (12-lead)",
      "quantity": "1.00",
      "unit_price": "450.00",
      "tax_rate": "0.00",
      "line_total": "450.00",
      "position": 1
    }
  ],
  "subtotal": "750.00",
  "tax_amount": "0.00",
  "discount_amount": "0.00",
  "total": "750.00",
  "amount_paid": "300.00",
  "balance_due": "450.00",
  "notes": null,
  "issued_at": "2026-09-30T05:05:00Z",
  "voided_at": null,
  "void_reason": null,
  "created_at": "2026-10-01T14:17:11Z",
  "updated_at": "2026-10-01T14:17:11Z"
}
```

`invoice_number` and `issued_at` are `null` on a draft. `items` come back in `position`
order. `patient_name` is denormalized in.

**`PATCH /invoices/{id}`** — drafts only. Body is `items` and/or `notes`; at least one is
required. **`items` replaces the whole line set**, it is not merged — send every line you
want to keep. `patient_id` and `appointment_id` cannot be changed.

**`GET /invoices`**

| Query param | Notes |
|---|---|
| `patient_id` | UUID |
| `status` | One of the six status values |
| `issued_from`, `issued_to` | `YYYY-MM-DD`, both **inclusive**, interpreted in the **hospital's timezone** — no offset parameter needed. Drafts have no issue date and never match. `issued_from` after `issued_to` is a `422` |
| `page`, `page_size` | As §1.6 |

Order is newest first by `created_at`. `data[]` is `InvoiceSummaryResponse` — no lines:

```json
{
  "id": "…",
  "invoice_number": "INV-2026-000003",
  "patient_id": "…",
  "patient_name": "Thomas George",
  "appointment_id": "…",
  "status": "partially_paid",
  "currency": "INR",
  "total": "750.00",
  "amount_paid": "300.00",
  "balance_due": "450.00",
  "issued_at": "2026-09-30T05:05:00Z",
  "created_at": "2026-10-01T14:17:11Z"
}
```

### 6.5 Issue

`POST /invoices/{id}/issue` — no body. Recomputes and freezes the totals, stamps
`issued_at`, and assigns the next number.

- Numbers are `INV-{year}-{seq:06d}`, **sequential and gap-free per hospital**. The
  sequence does not reset each year; `{year}` is the issue year in the hospital's
  timezone.
- A draft with no lines cannot be issued (`400`).
- A zero-total invoice goes straight to `paid`.
- After issue the invoice is immutable. A correction is a void plus a new invoice.

### 6.6 Payments

```
POST /api/v1/invoices/{invoice_id}/payments
Idempotency-Key: 9f1c4b2a-7d3e-4c1a-9b2f-0a1b2c3d4e5f
```

```json
{ "amount": "300.00", "method": "card", "reference": "CARD-TXN-8899", "notes": "Paid at the desk" }
```

`method` is `cash | card | upi | bank_transfer | insurance`. `amount` must be > 0 with at
most 2 decimals. `reference` (≤ 100) and `notes` (≤ 2000) are optional.

Response `data` is the payment **and** the invoice as it now stands, so the balance and
status can be updated without a second request:

```json
{
  "payment": {
    "id": "…",
    "invoice_id": "…",
    "amount": "300.00",
    "method": "card",
    "reference": "CARD-TXN-8899",
    "notes": "Paid at the desk",
    "received_by": "…",
    "received_at": "2026-09-30T05:10:00Z"
  },
  "invoice": { "…": "InvoiceSummaryResponse, as §6.4" }
}
```

Four things the client must get right:

1. **`Idempotency-Key` is required**, 16–100 chars (a UUID fits). Generate one per
   payment attempt and reuse it across retries of that attempt. A replay returns **200**
   with the original payment and the message "Payment already recorded with this key." —
   not 201, and not a second payment. Treat 200 and 201 as the same success path.
2. **Reusing a key for a different payment is `409`** — a different invoice, amount or
   method. Generate a fresh key for every new payment, including a second payment on the
   same invoice.
3. **Overpayment is `400`.** The amount cannot exceed `balance_due`. Two cashiers paying
   the same invoice at once are serialized by the server, so the second sees the reduced
   balance. On a `400`, re-fetch the invoice and show the current balance; do not retry
   blind.
4. Only `issued` and `partially_paid` invoices take payments. The invoice becomes
   `partially_paid`, or `paid` once `balance_due` reaches `"0.00"`.
5. **A cash-only user gets `403` for any other method.** A receptionist holds
   `invoice.payment.record.cash`, not `invoice.payment.record`. For them, offer only
   `cash` in the method selector rather than letting `card` or `upi` fail.

`GET /invoices/{id}/payments` returns the payments oldest-first as a plain list — **no
pagination metadata**, so use `http.get`, not `http.getPaginated`.

### 6.7 Void

`POST /invoices/{id}/void` with `{ "reason": "…" }` — required, 1–500 chars, not blank.

Allowed only while the invoice is `issued` **and has taken no payment**. An invoice with
any payment cannot be voided (`400`, "must be refunded first") — and refunds are not
built, so today a part-paid invoice cannot be undone through the API. The voided invoice
keeps its `invoice_number`; `voided_at` and `void_reason` are set. A draft cannot be
voided, and there is no way to delete one.

### 6.8 Invoices drafted from appointments

`POST /appointments/{id}/complete` now drafts an invoice automatically: one ad-hoc line,
`Consultation — Dr. {first} {last}`, at the doctor's `consultation_fee`, untaxed, linked
by `appointment_id`. Find it with `GET /invoices?patient_id=…&status=draft`.

- If the appointment already has a live invoice, nothing is drafted.
- The draft is created after the appointment is committed. A billing failure does not
  fail the completion — the response is still `200` and the invoice can be raised by hand.
- The complete response does **not** include the invoice id.

### 6.9 Roles → permissions

Which seeded role can call what. Hide controls per permission rather than letting the
call 403.

| Permission | Hospital Admin | Billing Staff | Receptionist | Doctor | Nurse |
|---|:--:|:--:|:--:|:--:|:--:|
| `service.read` | ✅ | ✅ | ✅ | — | — |
| `service.create` / `service.update` | ✅ | — | — | — | — |
| `invoice.read` | ✅ | ✅ | ✅ | — | — |
| `invoice.read.own` | ✅ | — | — | ✅ | — |
| `invoice.create` / `invoice.update` | ✅ | ✅ | — | — | — |
| `invoice.issue` | ✅ | ✅ | — | — | — |
| `invoice.void` | ✅ | — | — | — | — |
| `invoice.payment.record` | ✅ | ✅ | — | — | — |
| `invoice.payment.record.cash` | ✅ | — | ✅ | — | — |

**Two narrow codes, both enforced by the server:**

- **`invoice.read.own` — a doctor sees the invoices for their own visits only.** That
  means invoices linked to an appointment where they are the doctor. Another doctor's
  invoice for the same patient, and any invoice with no appointment, are not theirs. The
  list and its `total_records` contain only their invoices, and asking for any other
  invoice by id is a `404` identical to a missing one — so the client needs no special
  handling: call the same endpoints and render what comes back. A user with this code
  but no doctor profile gets an empty list.
- **`invoice.payment.record.cash` — a receptionist records cash only.** Any other
  `method` is `403 PERMISSION_DENIED`.

Things worth designing around: **only an admin can void**; a **receptionist can take a
cash payment but cannot create or issue** an invoice; and a **doctor is read-only**, with
no access to the services catalog — hide every billing action for them.

Either read code opens the Billing module. These replace the earlier `billing.read` /
`billing.write` placeholders, which the backend never issued. `invoice.approve_discount`,
`invoice.refund`, `invoice.pdf.download` and `invoice.ai_explain` are in the catalog but
guard nothing yet.

### 6.10 Not built yet

These paths from the module spec return `404`. Do not build against them:

- `POST /invoices/{id}/approve-discount` — and there is no way to set a discount at all
- `POST /invoices/{id}/refund`
- `GET /invoices/{id}/pdf`
- `POST /invoices/{id}/ai-explain`

---

## 7. Frontend ↔ backend mapping (mismatch resolution)

The audit flagged the patient contract as mismatched. It was investigated against
`API_CONTRACTS`/`05-DATABASE_DESIGN.md`, the module spec, the schemas and 944 passing
tests: **the backend contract is canonical and was not changed.** The frontend was wrong
and was corrected in `frontend/src/api/patients.ts`.

| Old frontend | Backend | Resolution |
|---|---|---|
| `name` | `first_name` + `last_name` + `full_name` | Use `full_name` for display; the create form collects both parts |
| `age: number` (input) | `date_of_birth` (input), `age` (computed output) | Form collects DOB; read `age` from the response |
| `gender: 'M' \| 'F'` | `male \| female \| other \| unspecified` | Frontend uses backend values; a `GENDER_LABELS` map handles display |
| `status: Outpatient \| Admitted \| Critical \| Discharged` | `active \| inactive` | **Dropped.** No admissions module exists; the values were invented by the mock |
| `doctor: string` | — | **Dropped.** Patients have no assigned doctor |
| `department: string` | — | **Dropped** from the patient shape; department lives on doctors |
| `?search=` | `?q=` | Renamed |
| `?pageSize=` | `?page_size=` | Renamed |
| — | `mrn` | Server-generated, read-only |

No backend field was renamed, no enum value was added, and no test was relaxed. Anything
the mock displayed that the backend does not model was removed from the UI rather than
faked.

---

## 8. Demo data

`make -C backend seed` — idempotent, safe to re-run; see
[10-DEVELOPMENT_GUIDE.md](10-DEVELOPMENT_GUIDE.md).

Seeded for the demo hospital (`demo-hospital`, timezone `Asia/Kolkata`):

- **6 departments**, one of them deactivated (Dermatology) so `include_inactive` is
  demonstrable.
- **5 doctors**, one deactivated. Four have a weekly availability schedule; the
  neurologist has a two-day leave block starting tomorrow, so the slots read model
  returns `on_leave` slots.
- **12 patients**, one deactivated. Ages 3–82, every gender enum value represented,
  varied blood groups, allergies and chronic conditions — enough to exercise `q`,
  `gender`, `age_gte`/`age_lte` and pagination.
- **14 appointments** covering all six statuses and all four types, spread across
  yesterday, today and the next two days, including two checked-in walk-ins for the
  queue. Each carries its full status history.
- **10 catalog services** across six categories — eight untaxed clinical services, one of
  them retired (`LAB-ESR`) so `is_active` is demonstrable, and two taxable non-clinical
  ones.
- **5 invoices**, one per state the UI has to render: `paid` (settled by cash and UPI),
  `void`, `partially_paid` (the re-issue of the voided visit, part-paid by card), `issued`
  (nothing paid), and a `draft` not tied to any appointment. They are numbered
  `INV-{year}-000001` to `000004`; the void invoice keeps its number.
- **3 payments** across three methods.

The in-flight appointments are deliberately left unbilled, so completing one in a demo
drafts its invoice live (§6.8).

Appointment times land on the doctors' published slot boundaries, so a seeded booking
shows up as `booked` in `GET /doctors/{id}/slots`. `doctor@demohospital.com` is Priya
Sharma, the seeded cardiologist, so signing in as her lands on a doctor with a real
schedule.

Demo logins (development only):

| Email | Password | Role |
|---|---|---|
| `admin@demohospital.com` | `Admin@1234567` | Hospital Admin |
| `doctor@demohospital.com` | `Doctor@1234567` | Doctor |
| `reception@demohospital.com` | `Reception@1234567` | Receptionist |

All seeded people are fictional. No real patient data exists in this repository.

---

## 9. Known gaps

Things the frontend will ask for that do not exist yet. Do not build against them:

- **Billing:** no discounts, refunds, invoice PDF or AI explain (§6.10). No way to delete
  or discard a draft invoice. A part-paid invoice cannot be voided or refunded (§6.7).
- **Billing roles:** "a doctor views invoices for their patients" is implemented as *their
  own visits* — not every invoice of a patient they have seen (§6.9). And
  `invoice.payment.record.cash` is a code the module spec's §10 does not list; it was
  added to express the spec's own cash-only rule for receptionists.
- **Billing tax:** there is no endpoint to set the hospital's tax rate, so every taxable
  line is taxed at 0% until one exists (§6.2).
- **Two shapes of `422`.** A request-validation error (wrong type, missing field) puts a
  list in `errors`, as §1.2 shows. A rule checked by the service — an unknown
  `patient_id`, an inactive service — puts an *object* there, with the list one level
  down at `errors.errors`. This is true of every module, not only billing. Read
  `message` for display; handle both shapes if you map errors to fields.
- No `sort` parameter on any of these endpoints (§1.8).
- No patient documents, timeline, or AI summary endpoints — they need object storage.
- No appointment token/queue-number field (§5.5).
- No patient admission/clinical status (§2.2).
- `metadata.request_id` is never populated on a success response (§1.2) — use the
  `X-Request-ID` header. Needs a team decision: populate it, or amend §5.1 of the
  standards doc.
- The refresh token is returned in the response body, not set as the HTTP-only cookie
  `CLAUDE.md` specifies (§1.4). Moving to a cookie is a backend contract change *and* a
  frontend change (drop the body field, send `credentials`), so it needs coordinating —
  do not half-migrate one side.

---

_Last updated: 2026-10-01. §6 (Billing) added with the module, and checked against the
running app and a freshly seeded database: every endpoint, permission, status code and
response shape in §6, the §6.9 table against the seeded roles, and the billing rows of §8.
Sections 6–8 of the previous revision are now §7–9._

_§2–5 were last re-verified on 2026-09-22 at commit `e3927e2`: every endpoint, permission,
query parameter, enum and response shape was checked against the running app, the §1.9
table against the seeded roles, and the demo data against a freshly seeded database. §1.4
was corrected then — it previously described a refresh-token cookie that the backend has
never set._
