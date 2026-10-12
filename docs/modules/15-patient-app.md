# 15 — Patient App

**Owner:** TBD
**Phase:** Patient App V1 (new phase, on top of the frozen Hospital V2 baseline)
**Status:** Approved / V1 Architecture
**Last updated:** 2026-10-06

> **Approved means the architecture is approved. Nothing in this document is built.** Every
> table, endpoint, service, package and screen named here is *planned*. Where this spec relies
> on something that exists today it says so and cites the file. Each capability is labelled
> **V1**, **V2** or **Future**:
>
> - **V1** — the first Patient App release. Blocked on the prerequisites in Section 26.
> - **V2** — the next release. Needs changes to the Hospital application or new
>   infrastructure that V1 deliberately avoids.
> - **Future** — depends on an external provider or a product decision that has not been made.
>
> This spec supersedes the "patient portal is future / v3" statements in
> `01-PRD.md` §10, `02-FEATURES.md` 4.13, `14-ROADMAP.md` and module specs 02, 03, 05, 06, 07,
> and the "separate Next.js app" note in `04-TECH_STACK.md` and `13-V1_TO_V2_MIGRATION.md`.
> Those documents and every Hospital V2 module spec have **not** been edited; the edits they
> need are listed in Section 29.4 and each requires its own approval.

---

## 0. Approved decisions

Recorded on 2026-10-06. These are settled; the rest of the spec implements them.

### 0.1 Architecture

| # | Decision |
|---|---|
| A1 | The Patient App is a separate frontend application in the same repository. |
| A2 | It shares the Atheris backend and platform. |
| A3 | It shares design tokens, UI primitives and contracts where appropriate. |
| A4 | A patient is a separate principal, **not** a staff RBAC role. |
| A5 | Patient APIs use a dedicated namespace: `/api/v1/patient/*`. |
| A6 | Patient authentication is phone OTP. |
| A7 | One patient account is designed to hold several patient-record links in the future; in V1 the only relationship is `self`. |
| A8 | Patient ownership is enforced on the server. |
| A9 | Health-record sharing uses first-class consent and access grants. |
| A10 | Patient AI reuses the existing Atheris AI runtime. |
| A11 | Hospital V2 stays frozen except for explicitly approved shared-platform and security dependencies. |

### 0.2 Decision 1 — OTP provider

- Phone OTP is required for V1.
- **No SMS provider is chosen.** The spec defines a provider-neutral sender interface
  (Section 5.9). The concrete provider adapter is an external dependency that can be added
  when a provider and credentials are approved, without changing the auth design.
- Credentials are configuration only, never in code.

### 0.3 Decision 2 — Patient linking

- V1 identity matching is: verified phone, then date of birth, then MRN **only** when more
  than one record still matches.
- A phone match alone never links an account to a record.
- The API never lists or describes candidate records.
- Unresolvable cases return a safe error; staff-assisted linking is V2.
- Outcomes are specified in Section 4.5.

### 0.4 Decision 3 — Documents

- V1 is **not** blocked on uploaded scans, arbitrary uploads, PDF generation or object storage.
- V1 records are prescriptions and released laboratory results.
- Document metadata and read access are in V1 **only where a real document source already
  exists. None exists today**, so V1 ships no document endpoints and no document storage
  (Section 10).
- V2: generated prescription and lab PDFs, uploaded scans, object storage, document versioning.

### 0.5 Decision 4 — Record access grants

- V1 backend: create, list and revoke access grants, and the authorization gate.
- The Hospital UI is not modified. The staff-side reader for granted records is V2.
- The gate is built complete in V1 — including the grantee-side checks the V2 reader will
  call — so adding the reader does not change the security model (Section 8.3).

### 0.6 Scope

| Phase | Capabilities |
|---|---|
| **V1** | Phone OTP login · policy consent · patient record linking · self-registration when no hospital record exists · patient home · hospital discovery · doctor discovery · availability · appointment booking · appointment self-cancellation · my appointments · read-only prescriptions · released-only laboratory results · access grants (create, list, revoke, gate) · document metadata/read only where an actual source exists |
| **V2** | Appointment rescheduling · contact-detail editing · staff-side record reader · staff-assisted linking and recovery · patient notifications and email · uploaded documents · generated PDFs |
| **Future** | Online payments · WhatsApp · dependents · conversational AI · native mobile apps · push notifications |

### 0.7 Security prerequisite classification

| Id | Issue | Classification |
|---|---|---|
| P1 | Base tenancy enforcement | **Must fix before Patient V1** — before any patient table or patient data exists |
| P2 | Plaintext MFA secret storage | **Must fix before Patient V1** — before new patient authentication is added |
| P3 | Cross-hospital staff login lookup | **Can be fixed during patient foundation** — it does not touch patient identity handling |
| P4 | Staff refresh-token / session architecture | **Can remain temporarily** for staff; the shared cookie mechanism is built during patient foundation |

Detail, tasks and dependencies: Section 26.

---

## 1. Product purpose

Give a patient a secure, self-service view of their own care at hospitals that run Atheris:
find a hospital and doctor, book an appointment, and read their own appointments,
prescriptions, released lab results and documents — on a phone, without calling the front desk.

The Patient App is a second frontend on the same backend. It introduces a second kind of
principal (the patient) next to hospital staff, and a consent model that puts the patient in
control of who else may see their records.

## 2. Scope

### 2.1 V1

| Area | V1 capability |
|---|---|
| Login | Phone number + one-time code. Session survives a page reload. |
| Policy consent | Accept versioned policies; consent to link a hospital record; consent to register at a hospital. |
| Record link | Link the account to the patient's existing record at a hospital (phone + date of birth, MRN on ambiguity). |
| Self-registration | Create a new patient record at a hospital when no record matches. |
| Home | Next appointment, active prescriptions, recently released results, linked hospitals, pending actions. |
| Hospital discovery | List and search hospitals that have enabled the Patient App. |
| Doctor discovery | Departments and doctors of one hospital, with a patient-safe profile. |
| Availability | Bookable slots for one doctor on one date. |
| Appointment booking | Book one available slot for oneself. |
| My appointments | List and view. |
| Self-cancellation | Cancel a booked appointment within the hospital's cut-off. |
| Prescriptions | Read-only. |
| Lab results | Read-only, released results only. |
| Access grants | Create, list, revoke; the authorization gate. No staff-side reader. |
| Documents | Metadata and read access only where a real source exists. **None exists today, so nothing ships** (Section 10). |

### 2.2 V2

- Appointment rescheduling.
- Contact-detail editing.
- Staff-side reader for records shared by an access grant (Hospital app change).
- Staff-assisted linking and account recovery (Hospital app change).
- Patient notifications: in-app centre and email.
- Uploaded documents, object storage, document versioning.
- Generated prescription and lab PDFs.
- Constrained, read-only Patient AI use cases (Section 19).

### 2.3 Future

- Online payments (Section 22).
- WhatsApp delivery (Section 23).
- Dependents / family members (Section 24).
- Conversational or tool-using Patient AI.
- Native mobile apps, push notifications, localisation beyond English.

## 3. Non-goals

- Not a staff tool. No staff screen, staff role or staff permission is added by this module.
- Not a second backend and not a second AI runtime.
- Not an emergency channel. The app must say so wherever booking is offered.
- No clinical advice, diagnosis, triage or interpretation of results.
- No patient-to-doctor messaging or telemedicine.
- No invoices, bills or payments in V1 or V2.
- No editing of clinical data by the patient at any phase.
- No patient access to another patient's data, including family members, in V1 or V2.
- No "Patient" role in staff RBAC (`roles` / `permissions` tables). `invoice.read.own`
  and `appointment.read.own` are staff permissions (a doctor's own visits) and must never
  be granted to, or reused for, a patient.

## 4. Patient principal and account model

### 4.1 Three distinct things

| Concept | What it is | Scope | Exists today |
|---|---|---|---|
| **Patient record** | A row in `patients`. Owned by one hospital, identified by MRN. | One hospital | Yes (`backend/app/models/patient.py`) |
| **Patient account** | A login identity for the Patient App, keyed by a verified phone number. | Platform-wide | No — planned |
| **Record link** | The verified statement "this account may act as this patient record". | One hospital | No — planned |

A patient account is **not** a `users` row and never appears in `users`, `user_roles`,
`refresh_tokens` or any staff table.

### 4.2 Why the link is a separate table

One person can be a patient at several hospitals (one record each), and — in the future — one
account can act for dependents. Putting a `patient_id` on the account, or an `account_id` on
`patients`, would make both impossible. The link table carries a `relationship` column from
day one; V1 accepts only `self`.

### 4.3 V1 cardinality

- One account per phone number.
- At most one **active** `self` link per (account, hospital).
- At most one **active** link per patient record (a record cannot be claimed by two accounts).
- An account may hold links at many hospitals.

### 4.4 Phone-binding rule

A `self` link is **honoured only while the linked record's `patients.phone` equals the
account's verified phone** (both E.164). The check runs on every request that resolves the
link. If the hospital changes the phone on the record, or the phone on the account no longer
matches, the link is treated as `suspended`: no data is served and the app asks the patient to
re-link. This is evaluated at read time so it needs no change to the frozen
`PatientService.update_patient`.

Reason: phone numbers are shared within families and are recycled by carriers. The phone on
the hospital's record is the only identifier the hospital itself verified in person.

### 4.5 Establishing a link (approved decision 2)

**Principle.** A verified phone number is necessary and never sufficient. An account is
linked to a record only when the verified phone **and** the date of birth identify exactly
one active record — with the MRN as a tie-breaker only when they do not. The API never
returns, counts or describes candidate records.

**Inputs.** The account's OTP-verified phone (from the session — never from the request
body), the `hospital_id` from the path, the `date_of_birth` the patient enters, and
optionally an `mrn`.

**Matching, inside the one hospital in the path:**

1. *Phone set* — records whose `patients.phone` equals the verified phone (E.164, exact).
2. *Match set* — records in the phone set whose `date_of_birth` equals the entered date.
3. If the match set has more than one record and an `mrn` was supplied, keep only the record
   with that MRN.

**Outcomes.**

| Case | Condition | Result | What the patient learns |
|---|---|---|---|
| **No match** | The phone set is empty | `404 RESOURCE_NOT_FOUND`. Self-registration is offered. | "We could not find a record with these details." |
| **Wrong date of birth** | The phone set is not empty, but no record in it has that date of birth | **The same `404` and the same message as no-match.** Self-registration is offered. | Nothing that distinguishes this from no-match — in particular, not that the phone is on someone's record |
| **Unique match** | Exactly one active record in the match set | `201` — the link is created, with the `hospital_record_link` consent | Their own record |
| **Ambiguous match** | More than one active record shares the phone and the date of birth, and no MRN was supplied | `409 LINK_MRN_REQUIRED`. No link. | Only that an MRN is needed |
| **Ambiguous, MRN supplied** | The MRN selects one record of the match set | `201` — linked | Their own record |
| **Ambiguous, MRN wrong** | The MRN matches no record in the match set | `404`, same as no-match. Self-registration is **not** offered after an MRN attempt. | Nothing |
| **Still unresolved** | Attempt limit reached (5 per hour, 10 per day, per account and hospital) | `403 LINK_UNAVAILABLE` until the window resets | "Please contact the hospital." Staff-assisted linking is V2. |
| **Inactive patient** | The only record matching phone + date of birth is soft-deleted (deactivated) | `403 LINK_UNAVAILABLE`. No link. **Self-registration is blocked**, so a deactivated record is not silently duplicated. | "Please contact the hospital." |
| **Already linked — same account** | The matched record is already actively linked to this account | `200` with the existing link (idempotent) | Their own record |
| **Already linked — this account, other record** | The account already has an active `self` link at this hospital to a different record | `409 RESOURCE_CONFLICT`. V1 allows one `self` link per hospital. | That they are already linked here |
| **Already linked — another account** | The matched record has a link from a different account | That link can only be stale: the phone-binding rule (4.4) has already suspended it, because the record's phone now equals this account's phone. The stale link is ended and the new link created, both audited. | Their own record |
| **Hospital not enabled** | Hospital inactive or the Patient App flag is off | `404` | Nothing |

Notes on the two cases that look alike:

- *Wrong date of birth* and *no match* are indistinguishable on purpose. A phone is often
  shared within a family, so "the phone exists but the date is wrong" would tell the caller
  that someone else's record is on that number.
- Because they are indistinguishable, a patient who mistypes their date of birth is offered
  self-registration. The registration screen therefore shows the entered date of birth for
  confirmation before creating a record, and the server re-runs the match inside the
  registration transaction (below).

**Self-registration** (V1) — `POST /patient/hospitals/{hospital_id}/register`:

- Allowed only when the match, re-run by the server in the same transaction, is *no match*
  or *wrong date of birth* for the submitted date of birth.
- Refused with `409` if a unique or ambiguous match now exists (the patient must link
  instead), and with `LINK_UNAVAILABLE` for the inactive-patient case.
- Creates a `patients` row through the existing `PatientService.register_patient`, using the
  account's verified phone — the client cannot supply a different phone — and links it.
- Requires the `hospital_registration` and `hospital_record_link` consents (Section 8.2).
- At most one self-registration per account per hospital.
- The audit event records whether other records already carried that phone, so hospital
  staff can review possible duplicates. No such review screen exists yet (V2).

**Staff-assisted linking** (V2) — a staff member links an account to a record after checking
identity in person. Needs a Hospital app screen. It is the resolution path for every
`LINK_UNAVAILABLE` case.

Every link attempt is rate limited and audited with its outcome (`patient.link.attempted`).
The audit row never contains the entered date of birth or MRN.

### 4.6 Planned tables

All follow `05-DATABASE_DESIGN.md`: UUID primary keys, `TIMESTAMPTZ` in UTC, audit columns.

| Table | Purpose | `hospital_id` |
|---|---|---|
| `patient_accounts` | Login identity: `phone` (unique, E.164), `status` (`active` / `suspended` / `closed`), `phone_verified_at`, `last_login_at`. | **None** — see note |
| `patient_otp_challenges` | One row per code request: `phone`, `code_hash`, `expires_at`, `attempts`, `consumed_at`, `ip_address`. | None |
| `patient_refresh_tokens` | Rotating refresh tokens for patient accounts; same shape as `refresh_tokens`. | None |
| `patient_account_links` | `account_id`, `patient_id`, `hospital_id`, `relationship` (`self` only in V1), `verified_via`, `linked_at`, `unlinked_at`. | Yes |
| `patient_consent_records` | Purpose-based consents (Section 8.2). | Nullable (some purposes are per hospital) |
| `patient_access_grants` | Record access grants (Section 8.3). | Yes (source hospital) |

> **Rule exception to approve.** `patient_accounts`, `patient_otp_challenges` and
> `patient_refresh_tokens` have no `hospital_id`. They hold platform-level identity data, not
> tenant data: an account exists before it is linked to any hospital. This is the same
> position as the `permissions` table. Architectural rule 4 ("every table with tenant data has
> a `hospital_id`") is not broken, but the exception must be stated in
> `05-DATABASE_DESIGN.md`.

`patient_documents` is **not** created in V1 (Section 10). No existing table is modified by this section. Modifications to existing tables are listed in
Section 26.

## 5. Authentication

### 5.1 Phone OTP (V1)

1. The patient enters a phone number. The client calls `POST /patient/auth/otp/request`.
2. The server normalises to E.164, applies the limits in 5.3, creates a challenge and sends
   the code by SMS. The response is identical whether or not an account exists for that number.
3. The patient enters the code. The client calls `POST /patient/auth/otp/verify`.
4. On success the server creates the account if it does not exist, marks the challenge
   consumed, and issues a patient access token (response body) and a refresh token (cookie).
5. If the current policy versions have not been accepted, the access token is still issued but
   every endpoint except policy acceptance, `GET /patient/me` and logout returns
   `CONSENT_REQUIRED` (Section 8.2).

### 5.2 Code properties

| Property | Value |
|---|---|
| Format | 6 digits from a CSPRNG (`secrets`) |
| Lifetime | 5 minutes |
| Use | Single use; consumed on first successful verify |
| Storage | Keyed hash (HMAC-SHA-256 with a server-side secret). Never stored or logged in plain text. A plain hash is not enough: the space is only 10⁶. |
| Binding | To the challenge id and the phone number. A code for one challenge never verifies another. |
| Verify attempts | 5 per challenge, then the challenge is dead |

### 5.3 OTP rate limiting and abuse prevention

The existing limiter (`backend/app/middleware/rate_limit.py`) keys only by IP, user and
hospital with one fixed window. It cannot express the limits below, so OTP limits are enforced
**in the patient auth service**, counted in Redis and keyed as shown. They apply in addition to
the global middleware.

| Limit | Key | Value |
|---|---|---|
| Resend cool-down | phone | 60 seconds between requests |
| Requests | phone | 3 per 15 minutes, 10 per 24 hours |
| Requests | IP | 10 per 15 minutes |
| Verify attempts | challenge | 5 |
| Failed verifies | phone | 10 per hour, then a 1-hour lock on that phone |
| Platform ceiling | global | Configurable sends per minute; exceeding it raises an operational alert and refuses new sends |

Other rules:

- Uniform responses and timing for known and unknown numbers.
- If Redis is unavailable the OTP endpoints **fail closed** (503). The middleware's in-process
  fallback is not acceptable here because every code costs money and a worker-local counter
  can be bypassed.
- Only numbers in an allow-list of country calling codes are accepted (default `+91`),
  configurable. This blocks international SMS-pumping fraud.
- No CAPTCHA in V1. If abuse is observed, adding one is a new-dependency decision.

### 5.4 Password fallback

**None.** Not justified:

- A password adds a second credential to store, reset and phish for users who will log in a
  few times a year.
- Recovery of a forgotten password would fall back to the phone anyway.

### 5.5 Sessions and refresh

| Item | Decision |
|---|---|
| Access token | JWT, 15 minutes, held in memory by the client. Never in `localStorage` or `sessionStorage`. |
| Refresh token | Opaque random token, stored hashed in `patient_refresh_tokens`, 7 days (project security rule 2). |
| Refresh transport | `HttpOnly; Secure; SameSite=Strict` cookie, `Path=/api/v1/patient/auth`, host-only (no `Domain`). Never in a response body. |
| Rotation | Every refresh issues a new token and revokes the old one. |
| Reuse detection | Presenting a revoked token revokes every session of that account and writes an audit event — same behaviour as staff (`auth_service.py:262-290`). |
| CSRF | The refresh and logout endpoints are the only cookie-authenticated endpoints. They require `SameSite=Strict`, an `Origin` check against the configured patient origins, and a custom request header. All other endpoints use the bearer token. |
| Reload | On load the client calls refresh; a valid cookie restores the session. |
| Logout | Revokes the current refresh token and clears the cookie. "Log out everywhere" revokes all. |

> Unresolved **U-9**: whether 7 days is right for patients, who log in rarely. A longer
> sliding lifetime needs an explicit exception to security rule 2. Until decided: 7 days.

### 5.6 Token audience and type

A patient access token is a different kind of token from a staff token and must be rejected
everywhere a staff token is expected, and vice versa.

| Claim | Staff token (today) | Patient token (planned) |
|---|---|---|
| `sub` | `users.id` | `patient_accounts.id` |
| `type` | `access` | `patient_access` |
| `aud` | absent | `atheris-patient` |
| `hospital_id` | the user's hospital | **absent** |
| `roles`, `permissions` | present | **absent** |

Consequences that must hold:

- `get_current_user` (`backend/app/api/dependencies/auth.py:57`) rejects a patient token: its
  `type` is not `access`, and PyJWT refuses a token carrying `aud` when the verifier supplies
  no audience.
- A patient principal must never be represented by an object whose `hospital_id` is `None`.
  In staff auth, `hospital_id is None` means Super Admin and bypasses every permission check
  (`auth.py:175`, `auth.py:221`).
- The patient verifier requires `aud == "atheris-patient"` and `type == "patient_access"`, so
  a staff token is rejected by patient endpoints.
- Both are implemented in `backend/app/core/security.py`, which is the only place JWTs may be
  signed or verified. That file is protected; the additions are listed in Section 26.3.

### 5.7 Tenant resolution

A patient account is not scoped to a hospital, so the token carries none.

- Every hospital-scoped route carries the hospital in the path:
  `/api/v1/patient/hospitals/{hospital_id}/…`.
- A single dependency resolves `(account_id, hospital_id)` to an active, non-suspended link
  and yields a **patient context**: `(account_id, hospital_id, patient_id)`.
- `patient_id` is **never** read from the body, the query string or the path.
- Cross-hospital list endpoints (`/patient/appointments`, `/patient/prescriptions`, …) iterate
  the account's active links on the server and call the tenant-scoped services once per link.
- No link → `404 RESOURCE_NOT_FOUND` for record endpoints. The response does not reveal
  whether the hospital holds a record for that phone.

### 5.8 Account recovery

| Situation | V1 behaviour |
|---|---|
| Lost device | Log in again with the phone; "log out everywhere" from the new device. |
| New phone number | The patient asks the hospital to update the phone on their record (an existing staff function). They then log in with the new number and link by phone + date of birth. The old account's link is suspended automatically by the phone-binding rule (4.4). |
| Number recycled to a stranger | The stranger can log in to the old account but every link is suspended as soon as any hospital updates the record, and links also fail the date-of-birth check if re-attempted. Accounts with no login for 12 months are closed and their links ended. |
| Suspected compromise | "Log out everywhere". Staff-initiated account suspension is V2. |

### 5.9 SMS provider abstraction (approved decision 1)

**No provider is chosen.** This section defines the smallest abstraction that lets the whole
OTP flow be built and tested now and a real provider be plugged in later without changing it.

#### 5.9.1 What the codebase already does

| Convention | Where | Applied here |
|---|---|---|
| One small `Protocol` plus a factory that returns `None` when the transport is not configured ("off unless configured") | `backend/app/core/email.py:45-57, 109-115` | The SMS sender follows the same shape |
| External HTTP APIs are called with `httpx`; no vendor SDK is installed | `backend/app/ai/providers/groq.py`; `backend/CLAUDE.md` | A provider adapter uses `httpx`. No new dependency |
| Every new dependency is a review decision | `CLAUDE.md`; `docs/04-TECH_STACK.md:3` | None is added |
| Secrets are `SecretStr` settings from the environment; nothing in code | `backend/app/core/config.py:218` (`GROQ_API_KEY`) | Provider credentials and the OTP hashing secret are `SecretStr` settings |
| Provider errors never reach a response or a log; typed errors with fixed messages | `backend/app/ai/errors.py` | Same for SMS |

#### 5.9.2 The abstraction

One new file, `backend/app/core/sms.py` (shared change S5):

| Element | Definition |
|---|---|
| `SmsMessage` | Frozen dataclass: `to` (E.164), `body`, `purpose` (`"otp"` is the only value in V1) |
| `SmsSender` | `Protocol` with one method: `async send(message: SmsMessage) -> None`. Raises `SmsDeliveryError` (fixed message, no provider text) on any failure. |
| `get_sms_sender()` | Factory. Returns `None` when `SMS_PROVIDER` is unset; otherwise the adapter registered for that name. |
| Dev sender | Selected by `SMS_PROVIDER=dev`. Allowed only when `APP_ENV=development`; a settings validator refuses to start the app with it in staging or production. |
| Provider adapter | **Not written.** When a provider is approved it is one class implementing `SmsSender` over `httpx`, one entry in the factory, and its settings. Nothing else changes. |

Planned settings (`backend/app/core/config.py`; names final at implementation):

| Setting | Type | Purpose |
|---|---|---|
| `SMS_PROVIDER` | `str \| None` | Selects the adapter. Unset = SMS off. |
| `SMS_API_KEY` | `SecretStr \| None` | Provider credential |
| `SMS_SENDER_ID` | `str \| None` | Sender identity, if the provider needs one |
| `SMS_TIMEOUT_SECONDS` | `float` | Send deadline (default 5) |
| `PATIENT_OTP_SECRET` | `SecretStr` | Key for the OTP keyed hash; separate from `APP_SECRET_KEY` |
| `PATIENT_OTP_TTL_SECONDS`, attempt and rate limits | `int` | The values in 5.2 and 5.3 |
| `PATIENT_OTP_ALLOWED_COUNTRY_CODES` | `list[str]` | Default `["+91"]` |

No credential has a default value. No credential is committed.

#### 5.9.3 Flow

```
Patient                Patient auth service                 SmsSender            Redis / DB
  │  request OTP  ───────────▶│
  │                           │ normalise phone; country allow-list
  │                           │ check limits (5.3) ─────────────────────────────▶ Redis
  │                           │ generate code (CSPRNG)
  │                           │ store keyed hash, expiry ───────────────────────▶ patient_otp_challenges
  │                           │ send(SmsMessage) ──────────▶ provider (external)
  │◀── 202 challenge_id ──────│
  │
  │  verify OTP  ────────────▶│
  │                           │ load challenge; check expiry, attempts, lock ───▶ DB / Redis
  │                           │ constant-time compare of keyed hashes
  │                           │ consume challenge; resolve or create account ───▶ patient_accounts
  │                           │ issue access token + refresh cookie ────────────▶ patient_refresh_tokens
  │◀── 200 session ───────────│
```

#### 5.9.4 Requirements

| Requirement | How it is met |
|---|---|
| **OTP expiry** | 5 minutes, stored on the challenge and checked on verify (5.2) |
| **Retry limits** | 5 verify attempts per challenge; resend cool-down of 60 seconds (5.2, 5.3) |
| **Rate limiting** | Per phone, per IP and a global send ceiling, counted in Redis by the auth service (5.3) |
| **Brute-force protection** | Attempt cap per challenge; per-phone failure lock; a code is bound to one challenge; keyed hash at rest; constant-time comparison; fail closed without Redis (5.3) |
| **No OTP logging** | The code exists in plain text only in memory and in the outbound provider request. It is never written to application logs, audit rows, error messages, traces or the database. `SmsMessage.body` is excluded from every log call; the sender logs a hash of the phone and the provider's message id only. |
| **Tenant-safe account resolution** | Requesting and verifying an OTP touch only the platform-level tables (`patient_otp_challenges`, `patient_accounts`). They never query `patients` or any hospital's data, never accept a `hospital_id`, and never reveal whether a phone is on any hospital's record. An account row is created only after a successful verify. Attaching the account to a hospital record is the separate, stricter linking step (4.5). |
| **No hard-coded credentials** | All provider settings come from the environment through `Settings` |

Other rules:

- If `get_sms_sender()` returns `None`, `POST /patient/auth/otp/request` returns
  `503 SERVICE_UNAVAILABLE` and creates no challenge. It never pretends a code was sent.
- The send is inline in the request with a short timeout. The 10-second notification cron is
  too slow for a login code. A provider failure returns `503`; the challenge is discarded.
- The message text is a fixed template containing the code and its lifetime. Nothing else —
  no name, hospital or clinical information.
- The dev sender is the single, bounded exception to "the code is never written anywhere":
  it writes the code to the local process's standard output so a developer can log in. It
  cannot be selected outside `APP_ENV=development`. Automated tests use an in-memory fake
  sender, not the dev sender.

#### 5.9.5 External dependency

| Item | State |
|---|---|
| Provider | Not chosen (U-1) |
| Credentials | None exist |
| Sender identity and message template approval | Not started; may have a lead time with the provider or regulator |
| What can proceed without it | All of Phases 1–7: the abstraction, the OTP service, sessions and the app, using the dev and fake senders |
| What cannot | The V1 release. There is no SMS fallback and no alternative login. |

## 6. Patient authorization

Staff authorization is role → permission. Patient authorization is different in kind:
**scope = self**. There are no patient permissions to grant or revoke.

1. **Authenticated.** A valid patient access token.
2. **Policies accepted.** Current policy versions accepted (Section 8.2).
3. **Hospital enabled.** The hospital is active and has the Patient App enabled (Section 11).
4. **Linked.** An active, non-suspended link exists for (account, hospital).
5. **Owner.** Every object returned or changed has `patient_id` equal to the context's
   `patient_id` and `hospital_id` equal to the context's `hospital_id`.
6. **State gate.** The object is in a state a patient may see (for example, a lab order is
   `released`).

Implementation rules:

- Steps 1–4 are one FastAPI dependency. Steps 5–6 are in the patient-facing service, not the
  router and not the existing staff service.
- Patient routers never import `get_current_user`, `require_permission` or
  `require_any_permission`.
- Patient routers never return a staff response schema. Every response is a patient DTO with
  an explicit allow-list of fields (Section 9).
- An ownership failure is `404`, never `403`, so the API does not confirm that an object exists.
- Existing services are called with the context's `hospital_id` and `patient_id`. They are
  reused, not modified, wherever possible (Section 9.3).

## 7. Tenant isolation

- The patient context always carries exactly one `hospital_id`, taken from the link — never
  from client input alone.
- Every service call made on behalf of a patient passes that `hospital_id`.
- A cross-hospital view is a server-side loop over links. There is no query that spans
  hospitals for a patient.
- `patient_account_links` and `patient_access_grants` carry `hospital_id`
  and every repository method on them takes it.
- Tests: for every patient endpoint, a patient linked at hospital A gets `404` for the same
  kind of object at hospital B, and for another patient's object at hospital A.

**Prerequisite.** Tenant filtering is not enforced by the base repository today
(Section 26.1, P1). A second principal type makes that more dangerous, so P1 must land before
any patient endpoint.

## 8. Consent and access-grant model

Three separate mechanisms. None is a single global boolean.

| Mechanism | Answers | Table |
|---|---|---|
| Ownership (Sections 4, 6) | "Is this the patient's own record?" | `patient_account_links` |
| Purpose consent (8.2) | "Has the patient agreed to this specific use, under this policy version?" | `patient_consent_records` |
| Access grant (8.3) | "Has the patient let *someone else* see specific categories of their records?" | `patient_access_grants` |

### 8.1 What does not need a grant

- The patient reading their own records through a valid link. That is ownership.
- Staff of a hospital reading that hospital's own records of the patient. The hospital is the
  custodian of the records it created; staff access is governed by staff RBAC and is unchanged
  by this module.

### 8.2 Purpose consent

One row per (account, purpose, hospital-or-none, policy version).

| Field | Meaning |
|---|---|
| `account_id` | Who consented |
| `purpose` | `terms_of_service`, `privacy_notice`, `hospital_record_link`, `hospital_registration` (V1). Reserved for later phases: `whatsapp_delivery`, `ai_assistance`. |
| `hospital_id` | Required for `hospital_record_link` and `hospital_registration`; null for platform policies |
| `policy_version` | The exact version of the text shown |
| `granted_at`, `withdrawn_at` | Timestamps (UTC) |
| `ip_address`, `user_agent` | Evidence of the act |

Rules:

1. Consent is an explicit action. No pre-ticked boxes; no consent implied by continuing.
2. Each purpose is asked separately. Declining an optional purpose never blocks the rest.
3. A new policy version requires fresh acceptance; old rows are kept, never overwritten.
4. `terms_of_service` and `privacy_notice` are required to use the app. Withdrawal closes the
   account session and blocks use until accepted again.
5. `hospital_record_link` is required per hospital before any record of that hospital is
   shown. Withdrawing it ends the link.
6. `hospital_registration` is required before a new record is created at a hospital. It
   records that the patient agreed to give that hospital their name, date of birth, gender and
   phone.
7. Withdrawal is as easy as granting and is available from the same screen.

> The policy texts themselves do not exist. Legal must supply them (**U-2**).

### 8.3 Access grant

An access grant lets a named recipient read named categories of one patient record.

| Element | Definition |
|---|---|
| **Who grants** | A patient account, acting for one linked patient record (`grantor_account_id`, `patient_id`). The account must hold an active `self` link to that record at grant time. |
| **Who receives** | `grantee_type` + `grantee_id`. V1 types: `hospital` (any staff member of that hospital who holds the corresponding staff permission) and `doctor` (one doctor). A grantee is always a hospital or a clinician registered on the platform — never an email address, link or anonymous party. |
| **Hospital context** | `hospital_id` — the *source* hospital that holds the records. `grantee_hospital_id` — the hospital the recipient belongs to. They differ for cross-hospital sharing. |
| **Appointment / context** | Optional `context_appointment_id` (an appointment of this patient at the grantee hospital) and a required `purpose_note` chosen from a fixed list (`consultation`, `second_opinion`, `continuity_of_care`). |
| **Record categories** | One or more of: `identity`, `medical_history` (allergies, chronic conditions, current medications), `appointments`, `prescriptions`, `lab_results`, `documents`. `documents` is defined but cannot be selected in V1, because no document source exists (Section 10). Stored as rows in a child table or an array of an enum — not free text. No category is implied by another. |
| **Record window** | Optional `records_from` / `records_to` to limit the grant to records in a date range. |
| **Grant timestamp** | `granted_at` (UTC). |
| **Expiry** | `expires_at` (UTC), required. Default 30 days, maximum 365. |
| **Revoke timestamp** | `revoked_at` (UTC), with `revoked_by_account_id` and optional `revoke_reason`. |
| **Status** | Derived, never stored: `revoked` if `revoked_at` is set; else `expired` if `expires_at <= now()`; else `active`. Deriving it keeps the status from disagreeing with the timestamps. |
| **Audit event** | `patient.access_grant.created`, `.revoked`, `.expired` (written by a sweeper), and `.used` on every read made under the grant. |

#### 8.3.1 Direct API enforcement

- One function is the only way to read a patient's records on a grant:
  `AccessGrantService.authorize(grantee, patient_id, source_hospital_id, category, record_date)`.
  It returns the grant that authorises the read or raises.
- It is called by the service method that returns the data, for **every** request. A grant
  check in the UI, in a router, or at list time only is not enforcement.
- It evaluates, against the database, at request time: grant exists; not revoked; not expired;
  grantee matches the authenticated staff user's hospital (and doctor, for a `doctor` grant);
  category included; record date inside the window; the grantor's link is still active.
- Grant state is never cached in a token, in the client, or in Redis.
- A read with no authorising grant returns `404`, not `403`.
- Every read made under a grant writes `patient.access_grant.used` with the grant id, the
  staff user, the category and the record id.

#### 8.3.2 Behaviour after revocation or expiry

- Effective on the next request. There is no grace period.
- The recipient immediately loses the ability to list or open the records.
- Information the recipient already viewed, and anything they recorded in their own hospital's
  records as part of care, is not erased; the patient is told this in plain words when
  revoking.
- The grant row and its audit history are kept. Grants are never hard-deleted.
- Ending a record link (or the link being suspended) makes every grant from that link
  unusable, without changing the grant rows.

#### 8.3.3 Phasing

Approved decision 4 (Section 0.5).

| Capability | Phase |
|---|---|
| Patient creates, lists and revokes grants | V1 |
| Expiry sweeper and grant audit events | V1 |
| The complete `authorize()` gate, including every grantee-side check in 8.3.1, with full tests | V1 |
| A staff-facing API and screen through which a grantee actually reads shared records | V2 — needs a Hospital app change |

In V1 a grant can be created and revoked but **no recipient can read on it yet**: the reading
side lives in the frozen Hospital application.

What makes the V2 reader an addition and not a rewrite:

- `authorize()` already takes the grantee as an argument — a staff user with a hospital and,
  where relevant, a doctor identity — and already evaluates the grantee, category, window,
  expiry, revocation and link state. V1 tests call it directly with staff grantees.
- The grant schema already records the grantee, both hospitals, categories, context and window.
  The reader needs no new column.
- The `patient.access_grant.used` audit event and its fields are defined now.
- The V2 reader is a new staff-side read path that calls `authorize()` first and then the
  existing tenant-scoped service **of the source hospital**. It adds a caller; it does not
  change the gate, the schema or the patient endpoints.
- No other staff read path is widened. A grant never makes a cross-hospital record appear in
  an existing staff endpoint.

## 9. Patient record access rules

### 9.1 Visibility by record type

| Record | Patient may see | Never returned to a patient |
|---|---|---|
| Own profile | Name, date of birth, gender, blood group, phone, email, address, emergency contact, allergies, chronic conditions, current medications, MRN | `notes` (administrative), audit columns |
| Appointment | Doctor, department, start/end, status, type, own reason, cancellation reason | `notes` (reception notes), `created_by`, status-history staff ids, any other patient's appointment |
| Doctor | Display name, specialisation, qualifications, department, languages, bio | `user_id`, email, licence number, leave reasons |
| Slot | Start, end | Slot status detail, `appointment_id` of a booked slot |
| Prescription | Medicine name, dosage, frequency, duration, instructions, quantity prescribed and dispensed, prescriber, date, status | `available_quantity` (live stock), batch ids, `dispensed_by`, invoice ids, cost prices |
| Lab result | Test name, value, unit, flag, reference range, released date, "amended" marker with date | Any order not `released`; order and item `notes`; `sample_id`; `released_by`; `amended_by`; previous values; `price`; `invoice_id` |
| Document | Nothing in V1 — no document source exists (Section 10) | — |
| Invoice, payment, refund | Nothing in V1 or V2 | Everything |

> Unresolved (**U-3**): whether the consultation fee is shown, whether a prescription's
> doctor notes are shown, and whether `critical` lab flags are shown without a
> clinician having spoken to the patient first. Until decided, the fee and doctor
> notes are **not** returned, and critical results are shown with a fixed "contact your
> doctor" line and no interpretation.

### 9.2 Rules

1. A patient sees only records whose `patient_id` and `hospital_id` match the patient context.
2. Soft-deleted records are not shown. A soft-deleted (deactivated) patient record cannot be
   linked, and an existing link to it is suspended.
3. Clinical data is read-only for patients in every phase.
4. Lists are paginated per `06-API_STANDARDS.md` and capped at 100 per page.

### 9.3 Reuse of existing services

The staff services take an explicit `hospital_id` and an optional `actor_id`; none takes a
`User`. They can be called from patient-facing services.

| Need | Existing method | Gap the patient service must close |
|---|---|---|
| Own profile | `PatientService.get_patient_details` | Patient DTO |
| Doctors, departments | `DoctorService.list_doctors` / `get_doctor_details`, department list | Patient DTO |
| Slots | `DoctorService.get_slots` (`doctor_service.py:973`) | Return available slots only; drop `appointment_id` |
| List own appointments | `AppointmentService.list_appointments(patient_id=…)` | Patient DTO |
| One appointment | `AppointmentService.get_appointment` | Ownership check — it is tenant-only |
| Book | `AppointmentService.book_appointment` | Section 13 |
| Cancel | `AppointmentService` cancel transition | Ownership, state and cut-off checks |
| Prescriptions | `DispensingService.list_prescriptions(patient_id=…)`, `get_prescription` | Ownership check on get; patient DTO |
| Lab results | `LabService.list_orders(patient_id=…, status=…)`, `get_order` | Force `status = released`; ownership check on get; patient DTO |

## 10. Document access

### 10.1 Decision and current state

Approved decision 3 (Section 0.4): V1 is not blocked on documents, and V1 offers document
metadata or read access **only where a real document source already exists**.

No source exists. The codebase has no document table, no upload handling, no object storage
client and no PDF generation; `python-multipart` is not installed; the `S3_*` keys in
`backend/.env.example` are commented out and read by nothing.

Therefore, in V1:

- No document endpoint is built. The routes in Section 27.8 are reserved, not implemented.
- No `patient_documents` table, no storage bucket and no placeholder storage is created.
- The Patient App shows no Documents screen. It does not show an empty one.
- The `documents` access-grant category is defined but cannot be selected (Section 8.3).
- A patient reads prescriptions and released lab results as structured data (Sections 15, 16).

If a real source is added before V1 ships, the rules in 10.3 apply to it unchanged and the
reserved routes are implemented against it.

### 10.2 Model (V2, planned)

`patient_documents`: `hospital_id`, `patient_id`, `document_type`
(`lab_report`, `prescription`, `discharge_summary`, `other`), `title`, `source_type` /
`source_id` (the lab order or prescription it came from, if any), `version`, `storage_key`,
`content_type`, `size_bytes`, `checksum_sha256`, `visible_to_patient`, audit columns, soft delete.

### 10.3 Authorization rules (binding whenever documents exist)

1. Documents are stored in private object storage. No bucket, prefix or object is public.
2. A document is fetched only through an authenticated API call that re-checks ownership (or
   an access grant with the `documents` category) **at the moment of download**.
3. The API either streams the bytes itself or redirects to a pre-signed URL that expires
   within 60 seconds and is bound to that single object. A URL is never stored, logged, sent
   by SMS, WhatsApp or email, or returned in a list response.
4. Storage keys are random and never contain a patient name, MRN or phone.
5. A document derived from a lab order is visible to the patient only when that order is
   `released`.
6. `visible_to_patient = false` hides a document from the patient regardless of ownership.
7. Every download writes an audit event (`patient.document.downloaded`).
8. Responses carry `Cache-Control: no-store` and `Content-Disposition: attachment`.
9. Uploads verify MIME type, size and extension (security rule 7) and are scanned before
   becoming visible.

### 10.4 Phasing

| Capability | Phase |
|---|---|
| Document metadata and read access against an existing real source | V1 — nothing to ship, because no source exists |
| Generated prescription PDFs and lab PDFs | V2 (needs a PDF renderer — a new dependency) |
| Uploaded scans, object storage, document versioning | V2 (needs storage infrastructure and a Hospital app screen) |
| Documents uploaded by the patient | Future |

## 11. Hospital discovery

- A hospital appears in the Patient App only if `hospitals.is_active` is true **and** the
  per-hospital flag `feature.patient_app.enabled` is exactly `true`. The flag uses the existing
  mechanism (`backend/app/core/feature_flags.py`) and must be added to `KNOWN_FLAGS`.
- If the flag is off, every `/patient/hospitals/{hospital_id}/…` route returns `404`, including
  for patients who already hold a link there.
- Discovery requires a patient login. It is not a public directory in V1.
- Search: by name (case-insensitive substring) and by city (`address->>'city'`). No
  geolocation, distance sorting or map in V1.
- Fields returned: `id`, `name`, `slug`, `address`, `phone`, `logo_url`, `timezone`, and
  whether the caller holds a link there. Never `settings`, `tax_id` or `email`.

Gap to note: only the seed can set a `feature.*` flag today — there is no super-admin toggle
endpoint (`backend/app/schemas/hospital.py:124-147`). Turning a hospital on is an operational
step until one exists.

## 12. Doctor discovery

- Scope: one hospital at a time. No cross-hospital doctor search in V1.
- Lists only active doctors who have at least one availability window.
- Filters: department, free-text on name and specialisation.
- Patient DTO per Section 9.1. The display name comes from the doctor's `users` row; nothing
  else from that row is returned.
- Departments: `id`, `name`, `description`. Never `phone_extension` or `email`.

## 13. Appointment booking

### 13.1 Rules

1. The patient books **for themself only**. `patient_id` comes from the patient context.
2. The hospital must be Patient App enabled and the patient must hold an active link there
   (linking or self-registering first if needed — Section 4.5).
3. The requested start and end must exactly match a slot that `DoctorService.get_slots`
   returns as `available` for that doctor and date, recomputed on the server at booking time.
   This closes two gaps in the staff booking path, which checks only weekly availability
   windows and not doctor leave or the slot grid (`appointment_service.py:477-483`,
   `1321-1346`), without modifying that path.
4. `type` is `new` or `follow_up`. `walk_in` and `emergency` are never accepted from a patient.
5. `reason` is optional, plain text, at most the existing `ReasonText` length. `notes` is not
   accepted.
6. `allow_override` is always false for a patient.
7. Booking policy, per hospital, read from `hospitals.settings` with these defaults:

   | Setting key | Default | Meaning |
   |---|---|---|
   | `patient_app.booking_horizon_days` | 30 | Furthest bookable date |
   | `patient_app.min_lead_minutes` | 60 | Earliest bookable start from now |
   | `patient_app.max_active_bookings` | 3 | Future `booked` appointments one patient may hold at this hospital |
   | `patient_app.cancel_cutoff_minutes` | 120 | Latest self-cancel before start |

8. The patient may not hold two overlapping appointments at the same hospital.
9. Double booking of a doctor is prevented by the existing database exclusion constraint
   (`migrations/versions/0008…:182-186`). A lost race returns `409 RESOURCE_CONFLICT` with a
   generic message. The conflicting appointment's details — which the staff error includes —
   are never returned.
10. `Idempotency-Key` is required. The server derives the stored key as
    `pt:{account_id}:{client_key}` so a patient-chosen key can never collide with a staff key
    or another patient's. On replay, the service also verifies the stored appointment's
    `patient_id` matches the caller before returning it. The staff replay path returns any
    appointment holding the key (`appointment_service.py:465-472`); the derived key keeps
    patient traffic out of that path's blind spot without changing it.
11. There is no slot hold or reservation. The slot is taken only when the booking commits.
12. No fee is charged or shown as payable at booking in V1.
13. The booking screen states that the app is not for emergencies.

### 13.2 Attribution

`appointments.created_by` and `appointment_status_history.changed_by` are foreign keys to
`users.id`. A patient is not a user, so both are `NULL` for a patient booking — which today
means "the system acted" (`backend/app/models/appointment.py:279-284`).

To keep staff able to tell a self-booked appointment from a system action, V1 needs:

- the audit actor generalisation (Section 26.2, S1), and
- an additive `appointments.source` column (`staff` default, `patient_app`) (Section 26.2, S4).

## 14. Appointment management

| Action | Phase | Rule |
|---|---|---|
| List | V1 | Upcoming and past, across all linked hospitals, newest relevant first. |
| View | V1 | Own appointments only. |
| Cancel | V1 | Only from `booked`; only before `cancel_cutoff_minutes`; reason chosen from a fixed list plus optional text. Cancelling after check-in is not allowed, although the staff state machine permits it. |
| Reschedule | V2 | Same doctor, same rules as booking. |
| Check-in, no-show, complete | Never | Staff only. |

Status changes made by staff (check-in, completion, cancellation, no-show) are visible to the
patient on the next load. There is no push or live update in V1.

## 15. Prescriptions

- Read-only, V1.
- Own prescriptions across linked hospitals: list and detail.
- All statuses are shown, labelled: `active`, `partially_dispensed`, `dispensed`, `cancelled`.
- Fields per Section 9.1.
- The app shows a fixed line that the prescription is a record, not an instruction to
  self-medicate, and to follow the doctor's advice.
- No refill request, ordering, delivery, drug-interaction check or substitution in any
  current phase.
- A downloadable prescription PDF is V2 (Section 10).

## 16. Laboratory results

- Read-only, V1.
- **Only orders whose status is `released`.** The staff read path returns values in any status
  (`lab_service.py:752-803`); the patient service filters by status in the query *and* checks
  it again on the single-order read.
- A cancelled order is not shown.
- Fields per Section 9.1.
- If a result was amended after release, the current value is shown with "amended on {date}".
  Previous values are not shown.
- Flags (`normal`, `low`, `high`, `critical`) are shown as recorded. The app adds no
  interpretation. See U-3 for critical results.
- No AI explanation of results in V1.

## 17. Patient home

One endpoint, `GET /patient/home`, composed on the server from the same patient services —
no new data and no separate access path.

- Next upcoming appointment (if any).
- Count and top three active prescriptions.
- Results released in the last 30 days.
- Linked hospitals, with any suspended link flagged.
- Pending actions: a policy version to accept, a link to re-verify, grants expiring within 7 days.

Each section fails independently: an error in one hospital's data shows that section as
unavailable rather than failing the whole page.

## 18. Notifications

| Capability | Phase | Notes |
|---|---|---|
| OTP by SMS | V1 | The only outbound message in V1. Sent directly by the auth service, not through the notification module. |
| "What's new" on home | V1 | Computed from data. No notification rows are written for patients. |
| In-app notification centre for patients | V2 | `notifications.recipient_user_id` is `NOT NULL` with a foreign key to `users.id` (`backend/app/models/notification.py:94-99`). A patient cannot be a recipient until that model is generalised (Section 26.2, S8). |
| Email to patients | V2 | SMTP sender exists; needs the recipient generalisation, patient-facing templates, and the worker in the deployment (it is not in `docker-compose.yml` today; Section 26.2, S9). |
| Appointment reminders | V2 | Needs scheduled sends, which no job does today. |
| SMS notifications beyond OTP | V2 | Same provider as OTP; needs templates and per-purpose consent. |
| WhatsApp | Future | Section 23. |
| Push | Future | No PWA or native wrapper exists. |

Staff are not notified of a patient booking in V1 beyond the appointment appearing in their
schedule.

## 19. Patient AI architecture

### 19.1 Constraint

There is one AI runtime: `backend/app/ai`, with one choke point,
`AIService.complete` (`backend/app/ai/services/ai_service.py:140`). **All patient AI goes
through it.** No second runtime, no second provider registry, no direct provider call from a
patient module, no model call from the frontend.

### 19.2 Current state of the runtime

It is a single-shot, stateless completion gateway with one consumer (staff slot suggestion).
It has no tools, chat, memory, redaction layer, output guardrails, budgets, durable
interaction log or evaluation harness. Its safety today comes from that one caller sending no
patient data and accepting no free text.

### 19.3 Phasing

| Phase | Patient AI |
|---|---|
| **V1** | **None.** |
| **V2** | Use cases shaped like the slot ranker only: the server assembles context for exactly one patient, there is no free-text input (or tightly bounded input), the output is constrained by a strict schema and re-validated on the server, the call is read-only, and it is a single call. Each is added as a prompt template under a patient namespace plus a use-case adapter, following `backend/app/services/slot_ranker.py`. |
| **Future** | Conversational or tool-using assistance. |

### 19.4 Runtime work required before any V2 patient use case

These are changes inside `backend/app/ai` — the same runtime, extended:

1. **Typed principal.** `AIService.complete` logs an untyped `actor_id`. It must record the
   principal kind so a patient id and a staff id are distinguishable.
2. **Self-scope bound on the server.** The patient's identity and hospital come from the
   patient context. They are never a model-supplied argument.
3. **Durable interaction record.** There is no `ai_interactions` table; today only a log line
   exists and it deliberately omits prompt and output.
4. **Budgets and separate capacity.** No budget is enforced, and one process-wide pool of 4
   concurrent calls is shared by everyone (`ai_service.py:271`). Patient traffic needs its own
   partition so it cannot starve staff, and a cost ceiling.
5. **Safe prompt rendering.** The renderer is sequential `str.replace`
   (`backend/app/ai/prompts/registry.py:99-112`) and is unsafe for untrusted text.
6. **Data minimisation.** A control in the runtime — not caller discipline — that limits what
   leaves for the provider to the authenticated patient's own data.
7. **Output policy.** No diagnosis, no treatment advice, emergency escalation text, and a
   refusal path. None exists.
8. **Evaluation.** A golden set and adversarial tests for each patient prompt. The harness
   does not exist.
9. **Decoupling.** The runtime reports "not configured" unless the staff slot prompt is
   present (`backend/app/ai/runtime.py:54`, `111-112`); patient use cases must not depend on it.
10. **Consent.** The `ai_assistance` purpose consent (Section 8.2) is required before any
    patient data is sent to a model.
11. **Provider terms.** Whether the current provider may be sent patient health data has not
    been assessed anywhere in the repository. It must be, before V2 (**U-8**).

### 19.5 AI access boundaries (all phases)

- AI acts with the patient's own scope and nothing wider. It can never read another patient's
  data, another hospital's data, or staff-only fields excluded by Section 9.1.
- AI never reads on an access grant.
- AI never mutates anything on a patient's behalf without an explicit confirmation step by the
  patient in the UI.
- A patient-facing AI feature is behind its own per-hospital flag, separate from
  `feature.ai.slot_recommendation`.
- An AI failure never blocks the non-AI path.

## 20. Audit requirements

### 20.1 Events

| Event | Trigger |
|---|---|
| `patient.auth.otp_requested` | A code is sent (phone stored hashed) |
| `patient.auth.otp_failed` / `otp_locked` | Wrong code / lock applied |
| `patient.auth.login` | Successful verify |
| `patient.auth.account_created` | First successful verify for a phone |
| `patient.auth.refresh_reuse_detected` | Revoked refresh token presented |
| `patient.auth.logout` / `logout_all` | Sessions ended |
| `patient.consent.granted` / `withdrawn` | Purpose consent change |
| `patient.link.attempted` / `created` / `ended` / `suspended` | Record link lifecycle |
| `patient.record.registered` | Self-registration created a patient record |
| `patient.access_grant.created` / `revoked` / `expired` / `used` | Grant lifecycle and every read under a grant |
| `patient.appointment.booked` / `cancelled` | Booking actions |
| `patient.document.downloaded` | Every document download — defined now, first emitted when documents exist (V2) |

Every mutating patient operation writes an audit entry (architectural rule 9).

> Unresolved **U-4**: whether each patient *view* of a prescription or lab result is also
> written to `audit_logs`, or only to structured access logs. The spec requires audit for
> grant-based reads and (when they exist) document downloads; plain self-views are logged,
> not audited, unless decided otherwise.

### 20.2 What must change in the audit module first

The audit module cannot represent a patient today (Section 26.2, S1):

- `AuditEvent.actor_id` is a user UUID or `None`, and the service writes
  `actor_type = "user" if actor_id else "system"` (`backend/app/services/audit_service.py:97`).
  A patient action would be recorded as the system.
- `audit_logs.actor_user_id` is a foreign key to `users.id`.
- `ip_address`, `user_agent` and `request_id` columns exist but are never populated.

Patient events must carry `actor_type = "patient"`, the patient account id, the hospital (when
known), the patient record id (when linked), IP, user agent and request id.

### 20.3 Rules

- Audit entries contain no OTP, no token, and no clinical values.
- Phone numbers in audit context are hashed or masked to the last four digits.
- Hospital staff with `audit.read` see patient events for their own hospital only.
- Account-level events with no hospital (login before any link) are visible to the platform
  operator only.

## 21. Security and privacy requirements

### 21.1 Patient ownership

- `patient_id` comes only from the server-resolved link.
- Every single-object read and every mutation re-checks `patient_id` and `hospital_id`.
- Ownership failures are `404`.
- A test per endpoint proves another patient in the same hospital cannot read or change the object.

### 21.2 Hospital / tenant isolation

- As Section 7. Prerequisite P1 (Section 26.1) lands first.
- Patient tokens carry no hospital and no roles; staff dependencies reject them (Section 5.6).

### 21.3 Consent enforcement

- Purpose consent and access grants are checked on the server, per request, against the
  database (Sections 8.2, 8.3.1).
- Withdrawal and revocation take effect on the next request.

### 21.4 Document authorization

- V1 serves no documents (Section 10). No storage, bucket or placeholder is created.
- Whenever documents exist, Section 10.3 is binding: private storage, check at download,
  short-lived single-object URLs, no links in messages, every download audited.

### 21.5 Rate limiting

The global middleware treats a request with no staff token as anonymous and keys it by IP at
60 per minute (`rate_limit.py:260-285`). Patient tokens are not recognised by
`AuthMiddleware`, so without a change every patient behind one mobile carrier NAT would share
one bucket. Required (Section 26.2, S2):

| Limit | Key | Proposed value |
|---|---|---|
| General patient API | patient account | 120 per minute |
| Per-hospital ceiling for patient traffic | hospital (from path) | Separate from the staff hospital ceiling, so patient traffic cannot exhaust it |
| Booking | patient account | 10 per hour |
| Link attempts | account + hospital | 5 per hour, 10 per day |
| Grant create / revoke | patient account | 20 per hour |
| OTP | Section 5.3 | |

### 21.6 OTP abuse prevention

As Section 5.3: per-phone, per-IP and global limits, attempt caps, uniform responses,
country allow-list, fail closed without Redis, keyed-hash storage, alerts on send spikes.

### 21.7 Session security

As Sections 5.5 and 5.6: short access token in memory, rotating `HttpOnly` refresh cookie
with reuse detection, strict audience and type, CSRF defence on cookie endpoints, origin
isolation from the staff app (Section 25.8).

### 21.8 AI access boundaries

As Section 19.5.

### 21.9 Privacy

- Data minimisation: every patient DTO is an allow-list. Adding a field is a spec change.
- No patient data in URLs or query strings beyond opaque ids.
- No PII in logs (security rule 10). Phones are hashed with the same approach as
  `AuthService._email_discriminator`.
- No third-party analytics, tracking or advertising scripts in the Patient App in V1.
- Fonts and other static assets are self-hosted for the Patient App; the staff app loads
  fonts from a third-party CDN today (`frontend/index.html:10-15`).
- No clinical data in SMS. An OTP message contains the code and nothing else.
- Account closure and data-export requests are handled manually in V1 through the hospital;
  self-service is V2. The legal basis and retention periods must be confirmed (**U-2**).
- CORS: patient origins are added explicitly to `CORS_ORIGINS`. Never `*`.
- Security headers on the Patient App: a Content-Security-Policy with no inline script,
  `frame-ancestors 'none'`, `Referrer-Policy: no-referrer`, HSTS.
- The compliance posture in `07-SECURITY.md` §16 stands: no certification is claimed.

## 22. Future payments

**Phase: Future. Nothing here is designed in detail and no provider is chosen.**

What exists: staff record payments manually. `payments.received_by` is `NOT NULL` with a
foreign key to `users.id`. `PaymentMethod` is a label. There is no gateway, payment intent,
pending or failed state, webhook, signature verification or reconciliation. The locking,
overpayment and idempotency core of `BillingService.record_payment` is sound and reusable.

Requirements when this is taken up:

- A real payment gateway. No simulated, stubbed or "test-only" payment flow may ship.
- **Provider not chosen.**
- A payment order/intent with explicit `created`, `pending`, `succeeded`, `failed`, `expired`
  and `refunded` states, separate from the invoice's own state.
- A webhook endpoint with signature verification, replay protection and idempotent handling.
  The webhook — not the browser redirect — is the source of truth.
- Reconciliation against the provider's settlement reports, with a report of mismatches.
- A successful payment is recorded through the existing billing core with a system or patient
  actor, which requires `received_by` to allow a non-staff actor (Section 26.2, S12).
- Refunds through the provider, tied to the existing refund model.
- No card data touches Atheris servers; use the provider's hosted fields or redirect.
- Patient access to invoices (issued only, never drafts) comes with this phase, with its own
  allow-list DTO.
- Regulatory and tax requirements for the target market to be confirmed with finance and legal.

## 23. Future WhatsApp delivery

**Phase: Future. No provider is chosen and nothing exists in code or configuration.**

Requirements when this is taken up:

- **Provider not chosen.** A business messaging provider with approved templates is required.
- **Consent required.** The `whatsapp_delivery` purpose consent, per account, withdrawable at
  any time, checked before every send.
- **Secure document delivery.** A document is never sent as an attachment and never as a
  direct file URL. A message carries only a notice and a link that opens the Patient App,
  where the patient authenticates and the download is authorised under Section 10.3.
- No clinical values, diagnoses or medicine names in message bodies.
- A channel-generic sender interface and a patient recipient in the notification model
  (Section 26.2, S8).
- Delivery receipts by provider webhook, with signature verification.
- Opt-out handling (for example a STOP reply) that withdraws the consent.
- Per-hospital flag, and per-hospital sender identity if the provider requires it.

## 24. Future dependent and family support

**Phase: Future. Not in V1 or V2.**

V1 is designed so that this does not need a rewrite:

- `patient_account_links.relationship` exists from day one; V1 accepts only `self`.
- The frontend keeps "which patient record am I acting as" in a separate session store
  (Section 25.4).
- Access grants name the patient record, not the account, so they work for a dependent.

Unsolved, and required before any dependent feature:

- Proof of guardianship or authority, verified by hospital staff.
- Age of majority handling: what happens to a parent's access when a child becomes an adult.
- Adolescent confidentiality rules.
- The phone-binding rule (4.4) applies to `self` links only; dependents need a different
  binding.
- Consent by a guardian on behalf of a dependent, and its withdrawal.

## 25. Frontend application architecture

### 25.1 Decision

A **second Vite application at `patient-app/`**, with its own entry point, router, auth
state, API client, build and deployment unit.

**The existing Hospital frontend is not extracted, refactored or touched in V1.** Nothing
under `frontend/` changes: not its source, its `package.json`, its lockfile, its Dockerfile
or its build. The shared Atheris design tokens and UI primitives start life as packages
*inside* the Patient App workspace; promoting them to repository level and moving the Hospital
app onto them is a V2 task with its own approval.

### 25.2 Location and workspace

```
/                                  (repo root — no root package.json is added)
├── frontend/                      Hospital app — UNCHANGED
└── patient-app/                   NEW — Patient App; also the npm workspace root
    ├── package.json               private; workspaces: ["packages/*"]
    ├── package-lock.json          the Patient App's own lockfile
    ├── index.html
    ├── vite.config.ts
    ├── src/                       the application (25.4)
    └── packages/
        ├── design-tokens/         @atheris/design-tokens — Tailwind v4 @theme tokens
        ├── ui/                    @atheris/ui — shadcn primitives
        └── api-core/              @atheris/api-core — envelope types, ApiError, error helpers,
                                   idempotency key, http-client factory
```

Why the workspace root is `patient-app/` and not the repository root:

- A repository-root `package.json` and lockfile would change how the Hospital app installs
  and builds, and would require edits to `frontend/Dockerfile`, `docker-compose.yml` and the
  existing CI job. That is exactly the destabilisation this decision rules out.
- A workspace is still needed: the shared packages import libraries such as `radix-ui`, and
  those imports only resolve when the packages and the app share one hoisted `node_modules`.
- Nesting the workspace under `patient-app/` gives that, with zero effect on `frontend/`.

### 25.3 Shared packages without touching the Hospital app

| Package | Seeded from (copied, at the Hospital V2 baseline commit) | Contents |
|---|---|---|
| `@atheris/design-tokens` | The `@theme` and `.dark` token blocks of `frontend/src/index.css` | Colour, type, spacing, radius tokens; dark-mode set |
| `@atheris/ui` | `frontend/src/components/ui/*` primitives | Button, input, dialog, sheet, select, tabs, etc. Not the staff data table, KPI tiles or detail cards |
| `@atheris/api-core` | `frontend/src/api/types.ts`, `frontend/src/api/idempotency.ts`, `frontend/src/lib/apiErrors.ts`, the envelope handling of `frontend/src/api/http.ts` | Principal-agnostic contract code only. No auth store, no token store, no Axios singleton |

Consequences, stated plainly:

- In V1 the tokens and primitives exist **twice**: in `frontend/` and in
  `patient-app/packages/`. They are a copy, not a shared import.
- Drift is controlled by a CI check in the Patient App job that compares the token blocks of
  `frontend/src/index.css` with `@atheris/design-tokens` and fails on any difference, so a
  token change in one place cannot silently diverge.
- The packages are written to be liftable: no import reaches outside the package, and no
  package imports from `patient-app/src`.
- **V2 task (needs approval):** move `patient-app/packages/*` to a repository-level workspace
  and switch the Hospital app to import them. Until then "shared" means "same source, kept
  identical by a check".

Dependencies: the Patient App uses the same libraries and major versions as the Hospital app
(`frontend/package.json`). No library is added that the Hospital app does not already use.
PWA tooling and an i18n library are not included in V1; adding either is a new-dependency
decision under `04-TECH_STACK.md`.

### 25.4 Patient App structure

```
patient-app/
├── index.html                 own title; self-hosted fonts
├── vite.config.ts             dev port 5174; /api proxy to the backend; alias @ → src
├── tsconfig*.json, eslint.config.js
└── src/
    ├── main.tsx               entry point
    ├── App.tsx                providers: theme, query client, session restore, router
    ├── router.tsx             the ONLY route table for the Patient App
    ├── layouts/
    │   ├── AuthLayout.tsx     phone entry, code entry
    │   └── PatientLayout.tsx  mobile-first shell with bottom navigation
    ├── routes/
    │   ├── RequirePatientSession.tsx
    │   └── RequirePolicyAcceptance.tsx
    ├── store/
    │   ├── patient-auth-store.ts      account identity, access token (memory), restoring flag
    │   └── patient-session-store.ts   linked hospitals and the active patient record
    ├── session/
    │   └── SessionRestorer.tsx        refresh-on-load from the cookie
    ├── api/
    │   ├── client.ts          Axios instance: bearer header, single-flight cookie refresh
    │   └── auth.ts, me.ts, consents.ts, links.ts, grants.ts, hospitals.ts, doctors.ts,
    │       appointments.ts, prescriptions.ts, labResults.ts, home.ts
    ├── pages/
    │   ├── auth/              phone entry, code entry
    │   ├── consent/           policy acceptance
    │   ├── home/
    │   ├── hospitals/         discovery, hospital detail, link, self-register
    │   ├── doctors/           list, profile
    │   ├── booking/           date and slot, confirm
    │   ├── appointments/      list, detail, cancel
    │   ├── prescriptions/
    │   ├── lab-results/
    │   └── profile/           profile, consents, access grants
    ├── components/            patient-specific components
    ├── lib/                   query client, formatting
    └── test/                  setup, fake API, fixtures
```

There is no `documents/` page folder in V1 (Section 10).

| Concern | Decision |
|---|---|
| Entry point | `patient-app/index.html` → `src/main.tsx` |
| Router | `src/router.tsx`, `createBrowserRouter`, lazy-loaded pages. Imports nothing from `frontend/`. |
| Patient auth store | `patient-auth-store.ts` (Zustand, not persisted). Holds the account, the in-memory access token and `isRestoring`. Clears the query cache when the identity changes. |
| Patient session handling | `SessionRestorer` calls refresh on load; the refresh token is an `HttpOnly` cookie, so a reload does not log the patient out. `patient-session-store.ts` holds the account's links and the active `(hospital, patient record)`; it is separate from auth so dependents can be added later without touching authentication. |
| Patient API client | `api/client.ts`, built from the `@atheris/api-core` factory. Base URL `VITE_API_URL ?? '/api/v1/patient'`. Attaches the bearer token; on `401` performs one shared refresh; ends the session only when the refresh itself is rejected. |
| Server state | TanStack Query, one hook per endpoint. |
| Forms | react-hook-form + zod. |
| Layouts | `AuthLayout` and `PatientLayout`. The staff `DashboardLayout` is not used. |

### 25.5 Routing separation

- Two apps, two route tables, two sets of guards, two builds. Neither imports the other's
  router, pages, layouts, stores or API client.
- An ESLint boundary rule in the Patient App forbids any import that resolves under
  `frontend/`; `patient-app/src` may import only from itself and `@atheris/*`.
- Planned routes: `/login`, `/verify`, `/consent`, `/` (home), `/hospitals`,
  `/hospitals/:hospitalId`, `/hospitals/:hospitalId/link`,
  `/hospitals/:hospitalId/doctors/:doctorId`,
  `/hospitals/:hospitalId/doctors/:doctorId/book`, `/appointments`,
  `/appointments/:hospitalId/:appointmentId`, `/prescriptions`,
  `/prescriptions/:hospitalId/:prescriptionId`, `/lab-results`,
  `/lab-results/:hospitalId/:orderId`, `/profile`, `/profile/consents`,
  `/profile/access-grants`.
- No staff route is reachable from the Patient App, and no patient route from the staff app.

### 25.6 Design rules

- Mobile-first from 360 px; touch targets at least 44 × 44 px (the copied button defaults to
  36 px high, so `@atheris/ui` adds a larger size variant); lists are cards, not horizontally
  scrolling tables; WCAG 2.1 AA contrast and focus states.
- Every screen has loading, empty and error states.
- English only in V1. User-facing strings live in one module per feature so an i18n library
  can be introduced later.
- No third-party scripts, analytics or font CDNs.

### 25.7 Environment and configuration

| Variable | Purpose |
|---|---|
| `VITE_API_URL` | API base. Defaults to `/api/v1/patient` (same origin, proxied). |
| `VITE_API_TARGET` | Dev proxy target only. |

No secret is placed in a `VITE_*` variable. There is no mock-auth switch in the Patient App.

In development the Vite proxy makes the API same-origin, so no change to `CORS_ORIGINS` is
needed to run the Patient App locally.

### 25.8 Deployment boundary

- The Patient App is a separate static build and a separate deployable.
- It is served from **its own origin** (for example a different subdomain from the staff app),
  so browser storage and cookies are isolated between the two.
- The API is reached same-origin through a reverse-proxy path (`/api`), as `12-DEPLOYMENT.md`
  describes for the staff app. This keeps the refresh cookie first-party and `SameSite=Strict`.
- Releasing the Patient App does not rebuild or redeploy the staff app.

Current state to be aware of: there is no production frontend build path in the repository at
all. `frontend/Dockerfile` is a dev image, `infra/` is empty, and the Nginx configuration that
`12-DEPLOYMENT.md` references does not exist. A production serving layer has to be built for
the Patient App; doing the same for the Hospital app is outside this spec.

### 25.9 Protected files this architecture requires changing

Kept to the minimum. Each needs explicit approval when the change is made:

| File | Change | When |
|---|---|---|
| `.github/workflows/ci.yml` | **Add** a `patient-app` job (install, lint, build, test, token-drift check). The existing backend and frontend jobs are not edited. | Phase 4 |
| `Makefile` | **Add** `patient-dev`, `patient-test`, `patient-lint`, `patient-build`. Existing targets unchanged. | Phase 4 |
| `backend/.env.example` | Document the new backend settings (Section 5.9, 26.2). | Phase 2 |
| `docs/04-TECH_STACK.md`, `docs/09-PROJECT_STRUCTURE.md`, `docs/12-DEPLOYMENT.md` | Record the second app. | On approval |

Not changed in V1: `docker-compose.yml`, `frontend/Dockerfile`, `frontend/package.json`,
`frontend/package-lock.json`, `.env.example` at the root, `frontend/.env.example`.

## 26. Shared-platform prerequisites

**None of this is implemented, and none of it is implemented by this spec.** Section 26.1
covers the four security issues found in the audit, with their approved classification, exact
tasks and dependencies. Section 26.2 lists changes the Patient App itself forces on shared code.

Classification meanings:

- **Must fix before Patient V1** — lands before the dependent patient work starts.
- **Can be fixed during patient foundation** — does not gate the start of patient work, but
  must be merged before the V1 release.
- **Can remain temporarily** — a known, recorded exception with an owner; not required for V1.

### 26.1 Security prerequisites

| Id | Issue | Classification | Gate |
|---|---|---|---|
| P1 | Base tenancy enforcement | Must fix before Patient V1 | Before the first patient table or migration |
| P2 | Plaintext MFA secret storage | Must fix before Patient V1 | Before patient auth code (S3) is written |
| P3 | Cross-hospital staff login lookup | Can be fixed during patient foundation | Before the V1 release |
| P4 | Staff refresh-token / session architecture | Can remain temporarily (staff side) | Shared cookie mechanism is built in patient foundation; staff migration is separate |

#### P1 — Base tenant enforcement · MUST FIX BEFORE PATIENT V1

**Problem.** The documentation says the base repository enforces `hospital_id` on every query
and that bypassing it is impossible (`CLAUDE.md`, `backend/CLAUDE.md`, `07-SECURITY.md` §4).
It does not. `backend/app/core/tenancy.py` does not exist, and
`BaseRepository.get_by_id`, `get_by_ids`, `list`, `update_by_pk`, `count` and `exists` apply
only the soft-delete filter (`backend/app/repositories/base.py:86-216`). Isolation depends on
each repository method being written with a `hospital_id` parameter and each caller passing
it. No cross-tenant leak was found in current callers, but nothing prevents one.

**Why it gates V1.** The Patient App adds a second principal type and new tables holding
patient identity, consent and grants. Those must not be built on a base that does not enforce
the boundary.

**Approved approach to confirm at task start (U-5).** Recommended: explicit `hospital_id`,
enforced — a tenant model cannot be read or written through the base repository without a
tenant, and a guard test proves it. PostgreSQL row-level security is a later second line of
defence (`07-SECURITY.md` lists it for v2.2).

**Implementation tasks.**

| Task | Work | Files |
|---|---|---|
| P1-T1 | Classify every model as tenant-scoped, nullable-tenant (`roles`, `audit_logs`) or tenant-less (`hospitals`, `permissions`, token tables, `mrn_sequences` keyed by hospital). Record the list in the task. | `backend/app/models/*` |
| P1-T2 | Make the base repository fail closed for tenant-scoped models: tenant-aware `get`, `list`, `update`, `count`, `exists`; a clearly named unscoped accessor for the legitimate exceptions. Decide whether `TenantMixin` or the presence of a `hospital_id` column is the marker. | `backend/app/repositories/base.py` (protected), `backend/app/models/base.py` |
| P1-T3 | Either create `core/tenancy.py` (if an ambient context is chosen) or remove every reference to it. | `backend/app/core/tenancy.py` (does not exist), docstrings in `patient_repository.py:11-14`, `department_repository.py:11-14`, `services/patient_service.py:12` |
| P1-T4 | Migrate the unscoped call sites. Each either passes a tenant or uses the unscoped accessor on purpose, with a comment saying why. | `services/auth_service.py:196,274,298,459,508,563,610,659,689`; `services/user_service.py:83,170,229,566`; `services/doctor_service.py:1056`; `services/notification_service.py:269,277`; `services/audit_service.py:123,219`; `api/dependencies/auth.py:116` |
| P1-T5 | Review all 24 repositories against the new base; align signatures. | `backend/app/repositories/*_repository.py` |
| P1-T6 | Move the two cross-hospital cron jobs to the unscoped accessor explicitly. | `backend/app/background/worker.py`, `background/jobs/*`, `repositories/notification_repository.py:290-321`, `repositories/appointment_repository.py:371` |
| P1-T7 | Tests: a tenant-isolation test for every repository method on a tenant model, and a guard test that fails when a tenant model exposes a public method that can run without a tenant. | `backend/app/tests/repository/*` |
| P1-T8 | Make the documentation match what was built. | `CLAUDE.md`, `backend/CLAUDE.md`, `docs/07-SECURITY.md` §4, `docs/05-DATABASE_DESIGN.md` (each needs approval) |

**Dependencies.** Upstream: approval to modify `repositories/base.py`; U-5. Downstream: blocks
Phase 3 onward (every patient table). Order: T1 → T2/T3 → T4, T5, T6 in parallel → T7 → T8.

**Done when.** The full Hospital V2 backend suite passes unchanged in behaviour, and the guard
test is in CI.

#### P2 — Plaintext MFA secret storage · MUST FIX BEFORE PATIENT V1

**Problem.** `users.mfa_secret` is documented as "Encrypted TOTP secret"
(`backend/app/models/user.py:114`) but is written and read in plain text
(`backend/app/services/auth_service.py:201-204, 627, 665, 696`). Anyone who can read the
`users` table can generate valid MFA codes for every enrolled staff member.

**Why it gates patient auth.** It is a live weakness in the same module that patient
authentication extends. The fix adds the project's only secrets-at-rest helper and a
dedicated key setting to `core/security.py` and `core/config.py`; patient auth (S3) adds
keyed OTP hashing and token functions to the same two files. Doing P2 first means that
protected file is changed once for key handling, with one reviewed pattern.

**Implementation tasks.**

| Task | Work | Files |
|---|---|---|
| P2-T1 | Add a dedicated encryption-key setting (`SecretStr`, separate from `APP_SECRET_KEY`), with support for a previous key during rotation. Fail startup in staging and production if it is unset. | `backend/app/core/config.py`; document in `backend/.env.example` (protected) |
| P2-T2 | Add authenticated encrypt/decrypt helpers with a version prefix on the stored value. `cryptography` is already a declared dependency — no new library. | `backend/app/core/security.py` (protected) |
| P2-T3 | Encrypt on write, decrypt on read. | `backend/app/services/auth_service.py`: `verify_mfa` (201–204), `enroll_mfa` (627), `confirm_mfa` (665), `disable_mfa` (696) |
| P2-T4 | New data migration that encrypts existing plaintext secrets. Idempotent (skips values that already carry the version prefix). State in the migration that downgrade needs the key. | `backend/migrations/versions/` (new file) |
| P2-T5 | Tests: round trip, wrong key, rotation with a previous key, migration on a mixed table, MFA login end to end. `core/security.py` has a 100% coverage floor. | `backend/app/tests/unit/…`, `tests/api/…` |
| P2-T6 | Fix the column comment and the security doc only if they no longer match. | `backend/app/models/user.py`, `docs/07-SECURITY.md` (needs approval) |

**Dependencies.** Upstream: approval to modify `core/security.py`; a decision on where the
key lives in each environment and how it is rotated (U-6); the key provisioned in every
environment **before** the deploy that contains T3–T4. Downstream: blocks S3 (patient token
and OTP functions). Order: T1 → T2 → T3 → T4 → T5.

**Done when.** No plaintext secret remains in `users.mfa_secret`, and existing MFA users can
still log in.

#### P3 — Cross-hospital staff login lookup · CAN BE FIXED DURING PATIENT FOUNDATION

**Problem.** Staff login finds the user with `select(User).where(User.email == email)` and
`scalar_one_or_none()` (`backend/app/repositories/user_repository.py:94-102`). Email is unique
only per hospital (`uq_users_hospital_email`). If the same email exists at two hospitals the
query raises and login fails with a server error. The query also skips the soft-delete filter.
Forgot-password uses the same lookup.

**Why it does not gate patient auth.** The condition set for "must fix" was that it affects
shared identity handling. It does not: patient identity is a separate table keyed by phone,
resolved without the `users` table or this lookup, and patient tokens are verified by a
separate function. P3 is a staff login defect. It is still a real defect and must be merged
before the V1 release, because V1 brings more hospitals onto the platform and with them a
higher chance of the same staff email at two hospitals.

**Implementation tasks.**

| Task | Work | Files |
|---|---|---|
| P3-T1 | Check every environment for staff emails that occur at more than one hospital. The result decides the approach. | (data check; no code) |
| P3-T2 | Make the lookup deterministic and exclude soft-deleted users. | `backend/app/repositories/user_repository.py:94-102` |
| P3-T3 | Apply the chosen approach (U-7): (a) platform-wide unique staff email — a new migration with a partial unique index on the lower-cased email of non-deleted users, no UI change, only possible if T1 finds no duplicates; or (c) keep the request shape and verify the password against each matching account with uniform timing, no UI change. Approach (b), a hospital identifier at login, changes the Hospital UI and is not recommended while it is frozen. | `backend/app/services/auth_service.py`: `login` (101), `forgot_password` (376), `_find_user_by_email` (732–740); `backend/migrations/versions/` (new file, approach a only) |
| P3-T4 | Tests: same email at two hospitals; soft-deleted user; forgot-password for both. | `backend/app/tests/…` |
| P3-T5 | Match the docs to the behaviour. | `docs/modules/01-authentication.md`, `docs/18-API_CONTRACTS.md` (each needs approval) |

**Dependencies.** Upstream: P3-T1; U-7. Independent of P1, P2 and every patient task. Can run
in parallel with Phases 2–5. Must be merged before Phase 9.

#### P4 — Staff refresh-token / session architecture · CAN REMAIN TEMPORARILY

**Problem.** Project rules require the refresh token in an `HttpOnly` cookie. The staff
implementation returns it in the JSON body (`backend/app/api/v1/auth.py:82,115,147`) and
accepts it in the body (`:137-138`, `:164`). The staff app keeps both tokens in JavaScript
memory (`frontend/src/services/tokenStore.ts`), so a page reload logs the user out and the
refresh token is readable by any script running in the page.

**Decision.** Staff behaviour stays as it is for now. Changing it alters Hospital frontend
behaviour and needs careful compatibility testing; it is not required for the Patient App,
which uses the cookie from day one (Section 5.5) through separate endpoints.

**What is done during patient foundation (part of S3).**

| Task | Work | Files |
|---|---|---|
| P4-T1 | Build the refresh-cookie mechanism once, in a form both principals can use: set, read, rotate, clear; `HttpOnly`, `Secure`, `SameSite=Strict`, scoped path; `Origin` and custom-header check. Used by patient auth only in V1. | `backend/app/core/security.py` (protected), `backend/app/core/config.py` |
| P4-T2 | Record the staff behaviour as a known, temporary exception to security rule 2's cookie requirement, with an owner and a target release. | `CLAUDE.md`, `frontend/CLAUDE.md` (each needs approval) |

**Staff migration — separate change, not part of Patient V1.**

| Task | Work | Files |
|---|---|---|
| P4-T3 | Backend, compatible step: set the cookie on login/refresh **and** keep returning and accepting the body token. | `backend/app/api/v1/auth.py`, `backend/app/schemas/auth.py`, `backend/app/middleware/cors.py` |
| P4-T4 | Hospital frontend: restore on load from the cookie; stop sending and storing the refresh token. | `frontend/src/services/tokenStore.ts`, `frontend/src/lib/api.ts` (55–57, 75–99), `frontend/src/components/auth/SessionRestorer.tsx` (38–48), `frontend/src/components/layout/Sidebar.tsx` (48), `frontend/src/store/auth-store.ts`, `frontend/src/test/fakeApi.ts` |
| P4-T5 | Backend, final step: remove the body token after every deployed frontend uses the cookie. | `backend/app/api/v1/auth.py`, `backend/app/schemas/auth.py` |
| P4-T6 | Compatibility tests: old frontend against new backend and new against old during the overlap; reuse detection; logout; multi-tab; reload. | Backend auth API tests, frontend auth tests |
| P4-T7 | Docs. | `docs/18-API_CONTRACTS.md`, `docs/modules/01-authentication.md` (each needs approval) |

**Dependencies.** P4-T1 depends on P2 (same protected file) and is required by Phase 3.
P4-T3 → T4 → T5 must ship in that order, across at least two releases. None of T3–T7 blocks
Patient V1.

**Accepted risk while it remains.** A script injected into the staff app can read the staff
refresh token; staff are logged out on reload.

### 26.2 Shared changes the Patient App itself requires

| Id | Change | Files | Phase |
|---|---|---|---|
| S1 | Audit actor generalisation: `actor_type = "patient"`, a patient account id on the event and the row, and population of IP, user agent and request id. | `backend/app/core/audit.py`, `backend/app/services/audit_service.py:94-105`, `backend/app/models/audit_log.py`, `backend/app/repositories/audit_log_repository.py`, `backend/app/core/constants.py`, `backend/app/schemas/audit.py`, a new additive migration | V1 |
| S2 | Recognise a patient principal for rate limiting and request logging; add patient tiers. | `backend/app/middleware/auth.py`, `backend/app/middleware/rate_limit.py:260-285`, `backend/app/middleware/logging.py`, `backend/app/core/config.py` | V1 |
| S3 | Patient token creation and verification, keyed OTP hashing, and the refresh-cookie helper (P4-T1). | `backend/app/core/security.py` (protected), `backend/app/core/config.py` | V1 |
| S4 | `appointments.source` (additive, default `staff`). | A new migration, `backend/app/models/appointment.py` | V1 |
| S5 | SMS sender interface and factory (Section 5.9). | `backend/app/core/sms.py` (new), `backend/app/core/config.py` | V1 |
| S6 | New error codes (Section 27.2) and the `feature.patient_app.enabled` flag. | `backend/app/core/error_codes.py`, `backend/app/core/feature_flags.py`, `backend/app/services/hospital_service.py` | V1 |
| S7 | Router registration for the patient namespace. | `backend/app/main.py`, `backend/app/api/v1/__init__.py`, `backend/app/api/dependencies/*` | V1 |
| S8 | Notification recipient generalisation (user **or** patient) and a channel-generic sender. | `backend/app/models/notification.py`, `backend/app/core/notifications.py`, `backend/app/services/notification_service.py`, `backend/app/repositories/notification_repository.py`, a new migration | V2 |
| S9 | Worker in the deployment. | `docker-compose.yml` (protected), deployment configuration | V2 |
| S10 | AI runtime generalisation (Section 19.4). | `backend/app/ai/**` | V2 |
| S11 | Repository-level frontend workspace; Hospital app adopts `@atheris/*` packages. | `frontend/**`, root `package.json`, `frontend/Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml` | V2 |
| S12 | `payments.received_by` to allow a non-staff actor. | `backend/app/models/billing.py`, a new migration, `backend/app/services/billing_service.py` | Future |

S1–S7 are additive: new columns with defaults, new functions, new files. No existing staff
endpoint, schema or screen changes behaviour. No existing migration is edited.

### 26.3 Protected files touched in V1

| File | By | Nature |
|---|---|---|
| `backend/app/repositories/base.py` | P1 | Behavioural change; highest review bar |
| `backend/app/core/security.py` | P2, S3 | Additive functions |
| `.github/workflows/ci.yml` | Section 25.9 | Additive job |
| `backend/.env.example` | P2, S3, S5 | New keys documented |
| Files under `docs/` | Section 29.4 | Documentation |

`backend/app/core/tenancy.py` is listed as protected but does not exist (P1-T3).
`docker-compose.yml` is not touched in V1. Each change above needs explicit approval at the
time it is made.

## 27. API plan (approved)

### 27.1 Conventions

- Prefix: `/api/v1/patient`. Routers will live in `backend/app/api/v1/patient/`.
- **No endpoint below exists.** This is the approved plan, not a description of the API.
- Envelope, pagination, timestamps and errors per `06-API_STANDARDS.md`.
- **Authentication** values:
  - `Public` — no token; rate limited.
  - `Cookie` — the refresh cookie, plus the `Origin` and custom-header check (Section 5.5).
  - `Patient*` — a patient access token; policy acceptance not required.
  - `Patient` — a patient access token **and** current policies accepted; otherwise
    `403 CONSENT_REQUIRED`.
- **Tenant resolution** values:
  - `None` — platform-level; no hospital involved.
  - `Path` — `hospital_id` from the path; the hospital must be active and Patient App
    enabled, else `404`. No link needed.
  - `Path → link` — as `Path`, and the server resolves the caller's active, non-suspended
    link at that hospital to a `patient_id`. No link → `404` (or `RECORD_LINK_REQUIRED` for
    booking).
  - `Links` — the server iterates the caller's active links and calls the tenant-scoped
    service once per hospital. The client supplies no hospital.
- **Ownership rule**: `Account` = the object belongs to the caller's account; `Own` = the
  object's `patient_id` and `hospital_id` equal the resolved patient context.
- `patient_id` is never accepted from the client on any endpoint.
- Every endpoint can also return `401 AUTHENTICATION_REQUIRED`, `422 VALIDATION_ERROR` and
  `429 RATE_LIMITED`. Only additional errors are listed.
- Ownership failures are `404 RESOURCE_NOT_FOUND`, never `403`.
- "Audit: none" means no audit-log row; the request is still in the structured access log.

### 27.2 New error codes

| Code | HTTP | Meaning |
|---|---|---|
| `CONSENT_REQUIRED` | 403 | A required policy or purpose consent has not been given |
| `RECORD_LINK_REQUIRED` | 403 | Booking needs a linked record at this hospital |
| `OTP_INVALID` | 401 | The code is wrong, expired or already used — one message for all three |
| `OTP_THROTTLED` | 429 | An OTP limit was reached; `Retry-After` is set |
| `LINK_MRN_REQUIRED` | 409 | More information is needed to identify the record; supply the MRN |
| `LINK_UNAVAILABLE` | 403 | Linking cannot be completed in the app; contact the hospital |

### 27.3 AUTH

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `POST /patient/auth/otp/request` | Request an OTP | Public | None. The phone is not matched to any hospital or patient record. | — | `phone` | `202`: `challenge_id`, `expires_in`, `resend_after`. Identical for known and unknown numbers. | `OTP_THROTTLED`; `503 SERVICE_UNAVAILABLE` (no SMS sender, or Redis down) | `patient.auth.otp_requested` |
| `POST /patient/auth/otp/verify` | Verify an OTP and start a session | Public | None. Resolves or creates the platform-level account for that phone only. | — | `challenge_id`, `code` | `200`: `access_token`, `expires_in`, `account`, `pending_policies[]`; sets the refresh cookie | `OTP_INVALID`, `OTP_THROTTLED` | `patient.auth.login`; `account_created`; `otp_failed`; `otp_locked` |
| `POST /patient/auth/refresh` | Rotate the session | Cookie | None | Account (token owner) | none | `200`: `access_token`, `expires_in`; rotates the cookie | `401` and the cookie is cleared | `patient.auth.refresh_reuse_detected` on reuse |
| `POST /patient/auth/logout` | End this session | Cookie | None | Account | none | `204`; clears the cookie | — | `patient.auth.logout` |
| `POST /patient/auth/logout-all` | End every session of the account | Patient* | None | Account | none | `204` | — | `patient.auth.logout_all` |
| `GET /patient/me` | Current patient: account, links, pending actions | Patient* | None for the account; `Links` for the link list | Account | — | `account` (id, masked phone, status), `links[]` (hospital id and name, `suspended`), `pending_policies[]` | — | None |

### 27.4 ACCOUNT / LINK

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `GET /patient/hospitals/{hospital_id}/profile` | Current patient profile at one hospital | Patient | Path → link | Own | — | Profile DTO (Section 9.1) | `404` | None |
| `POST /patient/hospitals/{hospital_id}/link` | Link to the patient's existing record | Patient | Path. The match runs only inside that hospital. | Verified phone + date of birth, + MRN on ambiguity (Section 4.5) | `date_of_birth`, `mrn?`, `consent_policy_version` | `201`: link (hospital, `linked_at`). `200` if this account is already linked to that record. Never any candidate detail. | `404 RESOURCE_NOT_FOUND` (no match — also wrong DOB and wrong MRN); `LINK_MRN_REQUIRED`; `409 RESOURCE_CONFLICT` (account already linked to a different record here); `LINK_UNAVAILABLE` (inactive record, or attempts exhausted); `CONSENT_REQUIRED` | `patient.link.attempted` (always, with outcome); `patient.link.created`; `patient.link.ended` when a stale link is replaced |
| `POST /patient/hospitals/{hospital_id}/register` | Self-register when no record matches | Patient | Path. Creates the record in that hospital only. | The server re-runs the match in the same transaction; registration proceeds only on no-match | `first_name`, `last_name`, `date_of_birth`, `gender`, `consent_policy_version` | `201`: link + profile DTO | `409 RESOURCE_CONFLICT` (a matching record exists — link instead; or already linked here); `LINK_UNAVAILABLE`; `CONSENT_REQUIRED` | `patient.record.registered`; `patient.link.created` |

There is no unlink endpoint. Withdrawing the `hospital_record_link` consent ends the link (27.5).
Contact-detail editing (`PATCH …/profile`) is V2.

### 27.5 CONSENT

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `GET /patient/consents` | Current policy versions and own consent records | Patient* | None | Account | — | `policies[]` (purpose, version, text reference), `records[]` | — | None |
| `POST /patient/consents` | Give a policy / purpose consent | Patient* | None for platform policies. For per-hospital purposes, `hospital_id` in the body is validated as active and enabled. | Account | `purpose`, `policy_version`, `hospital_id?` | `201`: consent record | `409 RESOURCE_CONFLICT` (version not current); `404` (hospital) | `patient.consent.granted` |
| `POST /patient/consents/{consent_id}/withdraw` | Withdraw a consent | Patient* | From the stored record | Account | — | Consent record | `404` | `patient.consent.withdrawn`; `patient.link.ended` when it ends a link |
| `POST /patient/hospitals/{hospital_id}/access-grants` | Create an access grant for records held at this hospital | Patient | Path → link (the source hospital). The grantee's hospital is validated as active and enabled. | Own (the grant is for the caller's linked record) | `grantee_type`, `grantee_id`, `categories[]`, `purpose_note`, `expires_at`, `context_appointment_id?`, `records_from?`, `records_to?` | `201`: grant with derived `status` | `404` (no link, or grantee not found); `400 BUSINESS_RULE_VIOLATION` (expiry out of range, category not available, context appointment not the caller's) | `patient.access_grant.created` |
| `GET /patient/access-grants` | List own grants | Patient | Links | Account | `status?`, pagination | Paginated grants with derived `status` | — | None |
| `POST /patient/access-grants/{grant_id}/revoke` | Revoke a grant | Patient | From the stored grant; the caller's link to it must exist | Account (grantor) | `reason?` | Grant | `404`; `409 RESOURCE_CONFLICT` (already revoked) | `patient.access_grant.revoked` |

The authorization gate (`AccessGrantService.authorize`, Section 8.3.1) is not an endpoint. It
is built and tested in V1 and has no HTTP caller until the V2 staff reader.

### 27.6 DISCOVERY

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `GET /patient/hospitals` | Discover hospitals | Patient | None; lists only active, enabled hospitals | — | `search?`, `city?`, pagination | Paginated hospital DTOs with `linked` | — | None |
| `GET /patient/hospitals/{hospital_id}` | One hospital | Patient | Path | — | — | Hospital DTO | `404` | None |
| `GET /patient/hospitals/{hospital_id}/departments` | Departments | Patient | Path | — | — | Department DTOs | `404` | None |
| `GET /patient/hospitals/{hospital_id}/doctors` | Doctors | Patient | Path | — | `department_id?`, `search?`, pagination | Paginated doctor DTOs | `404` | None |
| `GET /patient/hospitals/{hospital_id}/doctors/{doctor_id}` | One doctor | Patient | Path; the doctor must belong to that hospital | — | — | Doctor DTO | `404` | None |
| `GET /patient/hospitals/{hospital_id}/doctors/{doctor_id}/slots` | Bookable slots for a date | Patient | Path | — | `date` | `date`, `timezone`, `slots[]` of `start`, `end` — available slots only | `404`; `400` (date outside the booking horizon) | None |

Discovery returns reference data, not patient data, so it needs no link.

### 27.7 APPOINTMENTS

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `POST /patient/hospitals/{hospital_id}/appointments` | Create (book) | Patient | Path → link | Own: `patient_id` is forced to the caller's record | Header `Idempotency-Key`; `doctor_id`, `scheduled_start`, `scheduled_end`, `type` (`new` / `follow_up`), `reason?` | `201`: appointment DTO; `200` on idempotent replay | `RECORD_LINK_REQUIRED`; `404` (doctor); `409 RESOURCE_CONFLICT` (slot taken — no detail of the other booking); `400 BUSINESS_RULE_VIOLATION` (not an available slot, horizon, lead time, booking limit, own overlap) | `patient.appointment.booked` |
| `GET /patient/appointments` | List own appointments | Patient | Links | Own, per link | `scope` (`upcoming` / `past`), pagination | Paginated appointment DTOs, each with its hospital | — | None |
| `GET /patient/hospitals/{hospital_id}/appointments/{appointment_id}` | Detail | Patient | Path → link | Own | — | Appointment DTO | `404` | None |
| `POST /patient/hospitals/{hospital_id}/appointments/{appointment_id}/cancel` | Cancel | Patient | Path → link | Own | `reason_code`, `reason_text?` | Appointment DTO | `404`; `400 BUSINESS_RULE_VIOLATION` (not `booked`, or past the cut-off) | `patient.appointment.cancelled` |

Rescheduling is V2.

### 27.8 RECORDS

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `GET /patient/prescriptions` | List own prescriptions | Patient | Links | Own, per link | `status?`, pagination | Paginated prescription DTOs | — | None |
| `GET /patient/hospitals/{hospital_id}/prescriptions/{prescription_id}` | One prescription | Patient | Path → link | Own | — | Prescription DTO with items | `404` | None (U-4) |
| `GET /patient/lab-results` | List own released lab results | Patient | Links | Own, per link; `released` only | pagination | Paginated result summaries | — | None |
| `GET /patient/hospitals/{hospital_id}/lab-results/{order_id}` | One released lab order | Patient | Path → link | Own; `released` only | — | Result DTO with items | `404` (also when the order is not released) | None (U-4) |
| `GET /patient/documents` | List available documents | — | — | — | — | — | — | — |
| `GET /patient/hospitals/{hospital_id}/documents/{document_id}` | Document metadata | — | — | — | — | — | — | — |
| `GET /patient/hospitals/{hospital_id}/documents/{document_id}/content` | Download | — | — | — | — | — | — | — |

The three document routes are **reserved and not implemented in V1**: no document source
exists (Section 10). When one does, they are `Patient`, `Links` / `Path → link`, `Own` with a
re-check at download, return metadata without URLs, and audit every download.

### 27.9 HOME

| Endpoint | Purpose | Authentication | Tenant resolution | Ownership rule | Request | Response | Errors | Audit |
|---|---|---|---|---|---|---|---|---|
| `GET /patient/home` | Patient summary | Patient | Links | Own, per link | — | `next_appointment`, `active_prescriptions` (count + top 3), `recent_lab_results` (released in the last 30 days), `linked_hospitals[]`, `pending_actions[]` (policy to accept, link to re-verify, grants expiring within 7 days). Each section carries its own availability status. | — | None |

"Outstanding information" on home is limited to what is safely supported: appointments,
prescriptions, released results and consent actions. No balances, bills, payments, unreleased
results or notifications appear.

### 27.10 Backend structure (planned)

```
backend/app/
├── api/v1/patient/            routers: auth, me, consents, links, grants, hospitals,
│                              doctors, appointments, prescriptions, lab_results, home
├── api/dependencies/patient.py   get_patient_account, get_patient_context
├── core/sms.py                SmsSender interface and factory
├── services/patient_app/      patient_auth, otp, patient_account, record_link, consent,
│                              access_grant, patient_directory, patient_booking,
│                              patient_records, patient_home
├── repositories/              patient_account, patient_otp_challenge, patient_refresh_token,
│                              patient_account_link, patient_consent, patient_access_grant
├── models/                    patient_account.py, patient_consent.py
└── schemas/patient_app/       patient-facing DTOs only
```

Layering is unchanged: route → service → repository. Patient services call existing domain
services; they never call another module's repository.

## 28. Testing requirements and acceptance criteria

### 28.1 Testing

- Unit tests for every patient service method, happy and error path, with mocked repositories.
- Repository tests with a tenant-isolation case for every method on a tenant table.
- API tests per endpoint: success, `401`, `404` for another patient in the same hospital,
  `404` for the same patient context at another hospital, `422`, and `429` where a specific
  limit applies.
- Token separation: a staff token is rejected by every patient endpoint and a patient token
  by every staff endpoint. Run as a parametrised test over the whole route table.
- OTP: expiry, single use, attempt cap, each limit in 5.3, uniform responses, Redis-down
  behaviour, no code in any log line or audit row, behaviour with no sender configured.
- Linking: one test per outcome row in Section 4.5, plus a test that no response body ever
  contains a field of a candidate record.
- Refresh: rotation, reuse detection, cookie attributes, CSRF rejections.
- Consent and grants: every condition in 8.3.1, including the grantee-side checks, exercised
  directly against `authorize()`; revocation and expiry effective on the next call; a
  suspended or ended link disables grants.
- Lab: an unreleased order is never returned by list or by id.
- DTO allow-lists: a test per patient DTO asserting the exact field set, so an added staff
  field cannot leak silently.
- Audit: a test per mutating endpoint asserting the entry and `actor_type = "patient"`.
- Frontend: component and flow tests for each V1 screen; the import-boundary rule and the
  token-drift check in CI.

### 28.2 Acceptance criteria (V1)

- AC-1: A patient logs in with a phone number and code, and stays logged in after a reload.
- AC-2: A patient cannot reach any screen with data until the current policies are accepted.
- AC-3: A patient links to their existing record with phone + date of birth and sees only
  that record. A phone match without the right date of birth links nothing and reveals nothing.
- AC-4: A patient can never read or change another patient's data, in the same or another
  hospital, by changing any id in any request.
- AC-5: A patient token cannot call any staff endpoint, and a staff token cannot call any
  patient endpoint.
- AC-6: A patient books an available slot; a doctor on leave has no bookable slots; two
  patients racing for one slot produce exactly one booking.
- AC-7: A patient sees a lab result only after it is released.
- AC-8: A patient creates an access grant, sees it as active, revokes it, and
  `authorize()` refuses it on the next call.
- AC-9: Every mutating patient action appears in the audit log attributed to the patient.
- AC-10: Changing the phone on a hospital record suspends the link to the old account.
- AC-11: A hospital with the Patient App flag off does not appear and none of its patient
  routes respond.
- AC-12: The V1 change set contains no modification under `frontend/`, and the Hospital V2
  backend and frontend test suites pass.
- AC-13: No OTP value appears in application logs, audit rows or error responses.

## 29. Implementation plan

### 29.1 V1 implementation order

| Phase | Content | Depends on | Exit condition |
|---|---|---|---|
| **1 — Security prerequisites** | P1 (tenancy enforcement), then P2 (MFA secret encryption). Each is its own change with its own tests. | Approval to modify `repositories/base.py` and `core/security.py`; U-5, U-6 | Hospital V2 suite green; tenancy guard test in CI; no plaintext MFA secret |
| **2 — Platform foundation** | S1 audit actor · S2 patient-aware rate limiting · S3 patient tokens, keyed OTP hashing and cookie helper (P4-T1) · S5 SMS sender interface · S6 error codes and flag · S7 router registration | Phase 1 | Token-separation test passes over the full route table |
| **3 — Patient identity** | Accounts, OTP, sessions, policy consent, record linking, self-registration | Phase 2; a dev SMS sender is enough to build and test | AC-1, 2, 3, 5, 10, 13 |
| **4 — Patient App shell** | `patient-app/` workspace, `@atheris/*` packages, router, stores, API client, layouts, login and consent screens; CI job | Phase 3 for end-to-end login; the shell itself can start in parallel with Phase 2 | Login works end to end; AC-12 |
| **5 — Discovery and booking** | Hospitals, departments, doctors, slots, booking, my appointments, self-cancel; S4 | Phases 3, 4 | AC-6, 11 |
| **6 — Records and home** | Profile, prescriptions, released lab results, home | Phases 3, 4 | AC-4, 7 |
| **7 — Access grants** | Create, list, revoke, expiry sweeper, the complete `authorize()` gate | Phase 3 | AC-8 |
| **8 — Hardening and release** | P3 merged · real SMS provider adapter connected · security review · load test of OTP and booking · pilot at one hospital behind the flag | Phases 1–7; U-1, U-2 | AC-1 … AC-13 |

P3 runs in parallel with Phases 2–7 and must be merged before Phase 8 closes. P4-T2 (recording
the staff exception) is done in Phase 2. The staff session migration (P4-T3 … T7) is not in
this plan.

Each backend phase follows the 12-step module procedure in `10-DEVELOPMENT_GUIDE.md`.

### 29.2 V2 backlog

| Item | Needs |
|---|---|
| Appointment rescheduling | Patient booking service; no Hospital change |
| Contact-detail editing | A narrow patient DTO; interacts with the phone-binding rule |
| Staff-side reader for granted records | Hospital API and UI change; calls the existing `authorize()` gate |
| Staff-assisted linking and account recovery | Hospital API and UI change |
| Patient notifications: in-app centre and email | S8, S9; patient-facing templates; per-purpose consent |
| Appointment reminders | Scheduled sends; S8, S9 |
| Uploaded documents, object storage, document versioning | Storage infrastructure; Hospital upload screen; Section 10.3 |
| Generated prescription and lab PDFs | A PDF renderer (new dependency) |
| First constrained Patient AI use case | S10 and every item in Section 19.4; U-8 |
| Repository-level shared packages; Hospital app adopts `@atheris/*` | S11 |
| Staff session migration to the cookie | P4-T3 … T7 |
| Self-service data export and account closure | Legal input (U-2) |
| Super-admin toggle for the Patient App flag | A new platform endpoint |

### 29.3 External provider dependencies

| Dependency | Phase | Status | What is needed |
|---|---|---|---|
| **SMS provider (OTP)** | V1 | **Not chosen** | A provider with an HTTPS API reachable through `httpx`; approved sender identity and OTP message template; credentials supplied through configuration; a per-message cost ceiling. The design does not depend on which provider (Section 5.9). The V1 release cannot happen without it; development and testing can. |
| **Policy texts** | V1 | Not written | Terms of service, privacy notice, hospital record-link consent and hospital registration consent, each versioned, from legal (U-2). |
| **Object storage** | V2 | Not provisioned | Private bucket, server-side encryption, pre-signed URL support. |
| **PDF renderer** | V2 | Not chosen | A library; a new-dependency approval. |
| **Payment gateway** | Future | **Not chosen** | A real gateway, with webhooks and reconciliation (Section 22). |
| **WhatsApp business messaging provider** | Future | **Not chosen** | Approved templates, consent, secure document delivery (Section 23). |
| **AI provider review for patient data** | V2 | Not assessed | Whether the current provider may receive patient health data (U-8). |

No provider integration, credential or adapter exists in the repository for any of these.

### 29.4 Documents that this spec makes out of date

**Not edited.** Hospital V2 specifications are unchanged by this task. Each edit below needs
its own approval:

`01-PRD.md` §5, §6, §10 · `02-FEATURES.md` 4.11–4.13, 14.x · `03-ARCHITECTURE.md` ·
`04-TECH_STACK.md` · `05-DATABASE_DESIGN.md` · `06-API_STANDARDS.md` · `07-SECURITY.md` §3.2, §4 ·
`08-AI_ARCHITECTURE.md` · `09-PROJECT_STRUCTURE.md` · `12-DEPLOYMENT.md` · `14-ROADMAP.md` ·
`18-API_CONTRACTS.md` · module specs 01, 02, 03, 05, 06, 07, 11, 12, 13 · `CLAUDE.md` ·
`backend/CLAUDE.md` · `frontend/CLAUDE.md`.

## 30. Unresolved decisions

The architecture is approved. The items below are still open. None blocks the start of
Phase 1. Where a default is given, the spec applies it until the decision is made.

| Id | Decision | Needed by | Default applied until decided |
|---|---|---|---|
| U-1 | Which SMS provider, and its sender / template approval lead time | Phase 8 (release) | None — development uses the dev sender; release is blocked |
| U-2 | Policy texts, legal basis, retention periods, data-subject request process | Phase 3 (texts); Phase 8 (the rest) | Placeholder text is not acceptable in a release |
| U-3 | Patient-visible fields: consultation fee, a prescription's doctor notes, `critical` lab flags before a clinician has contacted the patient | Phases 5, 6 | Fee and doctor notes are not returned; critical results are shown with a fixed "contact your doctor" line and no interpretation |
| U-4 | Whether each patient self-view of a prescription or lab result is written to `audit_logs`, or only to access logs | Phase 6 | Access logs only; grant-based reads are always audited |
| U-5 | P1 approach: enforced explicit `hospital_id`, ambient tenant context, or row-level security | Phase 1 | Recommended: explicit and enforced now; RLS later |
| U-6 | P2 key location per environment and rotation procedure | Phase 1 | None — must be decided before P2-T1 |
| U-7 | P3 approach: platform-wide unique staff email, or try-each-match | Phase 8 | Depends on the data check (P3-T1) |
| U-8 | May the current AI provider receive patient health data | V2 AI | No patient data is sent to any model |
| U-9 | Booking-policy defaults (Section 13.1 rule 7) and the patient refresh-token lifetime | Phase 5; Phase 3 | The values in Section 13.1; 7 days |
| U-10 | Who switches a hospital's Patient App flag on, given no toggle endpoint exists | Phase 8 | An operational step for the pilot |

---

*The spec is the contract. Code follows spec. If reality demands a change, spec updates first.*
