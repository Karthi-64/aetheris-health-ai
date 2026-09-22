# 18 — Frontend API Contract Reference

The exact request/response contracts for the endpoints the frontend consumes today:
**Patients, Departments, Doctors, Appointments**. Written so a frontend module can be
built without reading backend code or guessing a shape.

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

Access token lives 15 minutes, in memory only. The refresh token is an HTTP-only cookie —
never read it from JS, never put either in `localStorage`. The 401-refresh-retry loop is
already implemented in `frontend/src/lib/api.ts`.

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
| `date` | Single calendar day, `YYYY-MM-DD` |
| `tz_offset_hours` | Integer −14…14, default **0**. Without it, `date` means a *UTC* day. See the caveat below |
| `status` | `booked \| checked_in \| in_progress \| completed \| cancelled \| no_show` |
| `type` | `new \| follow_up \| walk_in \| emergency` |
| `page`, `page_size` | As §1.6 |

`tz_offset_hours` is an **integer**, so a half-hour zone like IST (+5:30) cannot be
expressed exactly. Sending `5` covers 05:30–29:30 IST, which contains a normal clinic day
but is not the exact local midnight boundary. For a precise IST day, filter client-side, or
raise it with the backend before building a day view that depends on exact boundaries.
Flagged as a known limitation, not something to work around silently.

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

## 6. Frontend ↔ backend mapping (mismatch resolution)

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

## 7. Demo data

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

## 8. Known gaps

Things the frontend will ask for that do not exist yet. Do not build against them:

- No `sort` parameter on any of these endpoints (§1.8).
- No patient documents, timeline, or AI summary endpoints — they need object storage.
- No appointment token/queue-number field (§5.5).
- No patient admission/clinical status (§2.2).
- `tz_offset_hours` is integer-only, so exact IST day boundaries are not expressible (§5.3).
- `metadata.request_id` is never populated on a success response (§1.2) — use the
  `X-Request-ID` header. Needs a team decision: populate it, or amend §5.1 of the
  standards doc.

---

_Last updated: 2026-08-21. Contracts verified against the implementation at commit `fc7c1b4`._
