# Aetheris Health AI — Sprint Plan (v2.0)

**Living document. Owner: Sanjeev + tech lead. Updated at the end of every sprint.**

This is the sprint-level view of the v2.0 roadmap. Each sprint is 2 weeks. Build phase runs from **Jul 28 through Nov 30, 2026** — 9 sprints.

---

## Sprint Cadence

- **Length:** 2 weeks, Monday to Sunday
- **Planning:** Monday morning of sprint start
- **Standup:** daily, 15 min
- **Mid-sprint check:** Friday of week 1
- **Sprint review + retro:** Friday of week 2 (afternoon)
- **Demo:** Every sprint review includes a working demo of what was built

**Cardinal rule:** every sprint ends with something deployable to staging. No two-sprint features. If a feature can't fit in a sprint, break it into pieces that can.

---

## Delivery Overview

| Sprint | Dates | Track | Focus | Release Milestone |
|--------|-------|-------|-------|-------------------|
| 0 | Jul 28 – Aug 9 | Foundation | Infra, scaffolding, base patterns | — |
| 1 | Aug 10 – Aug 23 | **Alpha** | Auth, RBAC, MFA | — |
| 2 | Aug 24 – Sep 6 | **Alpha** | Users, Dashboard, Hospital Settings, Audit foundation | **Alpha release Sep 6** |
| 3 | Sep 7 – Sep 20 | **Beta** | Patient Management | — |
| 4 | Sep 21 – Oct 4 | **Beta** | Doctor Management, Appointments (start) | — |
| 5 | Oct 5 – Oct 18 | **Beta** | Appointments complete, multi-tenant validation | **Beta release Oct 18** |
| 6 | Oct 19 – Nov 1 | **RC** | Billing, AI Chat (M1), Function Calling (M2), Notifications base | — |
| 7 | Nov 2 – Nov 15 | **RC** | Reports, Hospital Memory (M3), RAG (M4), Notifications complete | — |
| 8 | Nov 16 – Nov 29 | **RC** | AI Observability (M5), E2E workflows, hardening, bug bash | **RC release + code freeze Nov 30** |

Post code freeze (Dec 1 – Dec 31): pilot beta with one hospital, hardening, load testing. **No new features.**

Production launch: **January 2027.**

### AI status note (2026-10-06)

The sprint sections below are kept as originally planned. This note records where the AI work actually stands; the full description is in [`08-AI_ARCHITECTURE.md`](08-AI_ARCHITECTURE.md) §0.

- **A real provider is connected.** Groq, called over `httpx` against its OpenAI-compatible API (no vendor SDK). The model is `openai/gpt-oss-20b`, overridable with `AI_FAST_MODEL`. The model named in earlier planning, `llama-3.1-70b-versatile`, was decommissioned by Groq. AI stays off on a server with no `GROQ_API_KEY`.
- **One AI capability is implemented: appointment slot recommendation** (Sprint 5 item) — `POST /api/v1/appointments/recommend-slot`, behind the `appointment.recommend_slot` and `doctor.availability.read` permissions and the per-hospital `feature.ai.slot_recommendation` flag.
- **The action is human-confirmed.** The AI suggests one free slot; a member of staff presses "Use this slot" and then "Book". Nothing is booked by AI.
- **Scope is intentionally narrow.** The model is shown only a date and one doctor's schedule as clock times — no patient data — and its answer is validated on the server. One call per request, with a timeout; no retry and no fallback provider.
- **Not built yet:** the patient history summary (Sprint 3 item), AI Chat and streaming (M1), Function Calling (M2), Hospital Memory (M3), RAG (M4), and the M5 items — the `ai_interactions` table, cost tracking, budgets and the eval harness. AI calls are recorded today as structured log lines only.

---

## Sprint 0 — Foundation (Jul 28 – Aug 9)

**Goal:** Nothing user-visible. Everything else in the plan depends on this being solid.

### Backend
- Monorepo layout finalized per `09-PROJECT_STRUCTURE.md`
- FastAPI skeleton with health checks (`/healthz`, `/readyz`, `/version`)
- SQLAlchemy + Alembic set up; first migration
- Base `Repository`, `Service`, `DI Container` classes
- Structured logging (JSON logs, request IDs)
- Error handling framework (custom exceptions → standard error responses)
- API versioning routing (`/api/v1/`)
- Response envelope wrapper
- Config management (`app/core/config.py` with Pydantic Settings)
- pytest scaffolding, factories/fixtures base

### Frontend
- Vite + React + TypeScript project
- Tailwind + Shadcn UI installed
- React Router set up
- React Query configured
- Base layout, theme, design tokens
- API client with response envelope handling
- Auth context skeleton (no logic yet)

### Infra
- `docker-compose.yml` with Postgres, Redis, backend, frontend, mailhog
- GitHub Actions CI: lint, typecheck, tests, build
- `Makefile` targets (per user preference: no shell scripts)
- Pre-commit hooks
- Env var management (`.env.example`, secrets in vault later)

### Deliverable
- `make up` starts the full stack locally
- Hitting `http://localhost:8000/healthz` returns 200
- Frontend loads a blank shell at `http://localhost:5173`
- CI passes on main

### Owners
- Backend infra: _[assign]_
- Frontend infra: _[assign]_
- CI/CD: _[assign]_

### Risks
- Team learning curve on new architecture (expect Sprint 0 to feel slow)
- Docker networking issues on team member laptops (address in daily standup)

---

## Sprint 1 — Alpha: Auth + RBAC (Aug 10 – Aug 23)

**Goal:** A user can log in, log out, get their permissions, and their session survives a refresh.

### Backend
- `auth` module complete per `01-authentication.md`
  - Login endpoint (email + password)
  - JWT access token (15 min) + refresh token (7 days) with rotation
  - Refresh token reuse detection
  - Password reset flow
  - Account lockout after N failed attempts
  - MFA (TOTP) — setup, verify, disable
- `rbac` foundations:
  - Roles table, permissions catalog
  - Permission check dependency for FastAPI routes
- Argon2id password hashing
- Structured audit log entries for every auth event

### Frontend
- Login page
- Password reset flow (request + confirm pages)
- MFA setup + verification pages
- Auth context (login, logout, refresh, permission checks)
- Protected route wrapper
- Auto-refresh token before expiry
- 401 handler → redirect to login

### Testing
- Unit tests: password hashing, token generation, permission check
- Integration tests: full login flow, refresh flow, lockout, MFA setup
- Security test: SQL injection on login endpoint

### Deliverable
- Demo: user registers (via seeded superadmin), logs in, sets up MFA, logs out, logs back in
- All auth endpoints documented in OpenAPI
- Passing security review

---

## Sprint 2 — Alpha: Users + Dashboard + Hospital Settings (Aug 24 – Sep 6)

**Goal:** Alpha release. Hospital Admin can provision users, users see role-appropriate dashboards, multi-tenancy is provably isolated.

### Backend
- `user_management` module complete per `02-user-management.md`
  - User CRUD
  - Invite flow (invite email → set password → activate)
  - Role assignment
  - User search/filter
  - Deactivate/reactivate
- `hospital_settings` module complete per `14-hospital-settings.md`
  - Hospital record CRUD
  - Working hours
  - Feature flags framework
  - Branding fields
- `audit_logs` foundation per `12-audit-logs.md`
  - Immutable table with REVOKE UPDATE/DELETE
  - Actor + IP + user-agent + request_id captured
  - Middleware to auto-log mutations

### Frontend
- User Management pages (list, create/invite, edit, deactivate)
- Hospital Settings page (all tabs from module spec)
- Dashboard shell:
  - Superadmin dashboard (stub)
  - Hospital Admin dashboard (stub)
  - Doctor dashboard (stub)
  - Reception dashboard (stub)
  - Role-based routing
- Audit log viewer (basic table)

### Testing
- **Multi-tenant isolation integration test** — create 2 hospitals, verify cross-tenant read returns empty
- E2E test: **Workflow 3 (Admin Journey)** — hospital setup → user creation → configuration
- Performance: auth endpoints < 200ms p95

### Alpha Release Gate (Sep 6)
✅ All success metrics for Alpha met (see roadmap):
- Auth endpoint response < 200ms p95
- Test coverage on auth flows = 100%
- Multi-tenant isolation test passing
- 0 critical security issues
- 0 open P1 bugs
- Deployed to internal staging

---

## Sprint 3 — Beta: Patient Management (Sep 7 – Sep 20)

**Goal:** Full patient registration and search working. MRN generation transactional. Documents attached.

### Backend
- `patient_management` module complete per `03-patient-management.md`
  - Patient CRUD
  - MRN generation (per hospital pattern, transactional, gap-free)
  - Medical history (structured)
  - Document upload → object storage (S3 or MinIO)
  - Patient search (by name, phone, MRN)
  - Basic AI: patient history summary (streaming SSE) — _status 2026-10-06: not built; see the AI status note above_

### Frontend
- Patient registration form
- Patient list with search + filters
- Patient detail page
- Medical history tabs
- Document upload UI

### Testing
- E2E: **Workflow 1 partial** — patient registration only
- Load test: 1000 patient search < 300ms p95

---

## Sprint 4 — Beta: Doctors + Appointments (start) (Sep 21 – Oct 4)

**Goal:** Doctors have profiles + availability. Basic appointment booking works.

### Backend
- `doctor_management` complete per `04-doctor-management.md`
  - Doctor profile linked to user
  - Weekly availability
  - Leaves
  - Consultation fee configuration
  - Slot computation (derived, cached)
- `appointment_management` — Phase 1 per `05-appointment-management.md`
  - Appointment state machine (draft → scheduled → in_progress → completed / cancelled / no_show)
  - Book appointment
  - Cancel appointment
  - Reschedule appointment
  - Idempotency key on booking

### Frontend
- Doctor management pages
- Doctor availability editor (weekly grid)
- Appointment calendar view (day/week)
- Book appointment flow
- Reschedule flow

### Testing
- E2E: **Workflow 2 partial** — doctor login → view schedule
- Integration: doctor availability + slot computation correctness

---

## Sprint 5 — Beta: Appointments complete + validation (Oct 5 – Oct 18)

**Goal:** Beta release. Full clinical scheduling working with production-grade constraints.

### Backend
- `appointment_management` — Phase 2
  - Walk-in flow (immediate slot)
  - Double-booking prevention (PostgreSQL `EXCLUDE` constraint)
  - No-show sweeper (background job)
  - AI slot recommendation (basic) — _status 2026-10-06: implemented as one suggested slot, confirmed and booked by a member of staff; see the AI status note above_
- Multi-tenant isolation validation across all 6 modules built so far
- Performance tuning: query indexes, N+1 elimination

### Frontend
- Walk-in booking UI
- Appointment status transitions
- Conflict resolution UI (when double-booking attempted)
- Appointment detail page

### Testing
- **All Beta E2E workflows passing**
- Load test: 100 concurrent users on staging
- 3 test hospitals provisioned in staging, each with isolated data
- Security scan: OWASP Top 10

### Beta Release Gate (Oct 18)
✅ All success metrics for Beta met:
- 3 test hospitals in staging
- 99.5% staging uptime
- CRUD response < 300ms p95
- 100 concurrent users supported
- E2E Workflows 1 and 2 passing
- < 5 P1 bugs
- < 20 P2 bugs

---

## Sprint 6 — RC: Billing + AI Chat + Function Calling (Oct 19 – Nov 1)

**Goal:** Money flows. AI is talking and taking actions.

### Backend
- `billing` module complete per `06-billing.md`
  - Services catalog
  - Invoice lifecycle (draft → issued → paid / void / refunded)
  - Sequential invoice number generation (gap-free, transactional)
  - Idempotent payment recording
  - Discount approval workflow
  - Refunds
  - Money as NUMERIC(15,2) throughout — no floats anywhere
- `ai_assistant` module Phase 1 per `13-ai-assistant.md`
  - **AI Milestone 1: AI Chat** — provider abstraction, Groq default, streaming SSE, session/message persistence
    - _Status 2026-10-06: the provider abstraction and the Groq connection already exist (model `openai/gpt-oss-20b`, via `httpx`); chat, streaming and session/message persistence are not built._
  - **AI Milestone 2: Function Calling** — typed tools wrapping module services (patient search, appointment list, doctor availability), permission-checked, audit-logged
- `notifications` base — templates, in-app, email

### Frontend
- Billing UI: create invoice, add line items, apply discount, record payment
- Invoice PDF generation and download
- AI chat widget (side panel), streaming responses
- Tool call visualization ("AI is searching patients…")
- Notification bell + inbox

### Testing
- Idempotency test: same payment key posted twice → single payment
- Invoice number gap-free test: parallel invoice creation
- AI eval: basic tool-calling test set (10 scenarios)
- E2E: consultation → billing partial

---

## Sprint 7 — RC: Reports + AI Memory + RAG (Nov 2 – Nov 15)

**Goal:** Analytics live. AI remembers context. AI answers from hospital documents.

### Backend
- `reports_dashboard` complete per `10-reports-dashboard.md`
  - Admin dashboard: patient count, appointments today, revenue, top diagnoses
  - Doctor dashboard: today's appointments, patient list
  - Reception dashboard: check-ins, pending payments
  - Billing dashboard: revenue trends, outstanding
  - CSV/PDF export
  - Redis caching layer with invalidation
  - AI-generated natural language dashboard summary
- `notifications` complete
  - Preferences per kind
  - Delivery log with retries
  - Critical kinds override preferences
- `ai_assistant` Phase 2:
  - **AI Milestone 3: Hospital Memory** — Redis-backed conversational memory, scoped to hospital + user + session, context window management, retention policy
  - **AI Milestone 4: RAG** — pgvector integration, embedding pipeline for hospital documents (SOPs, formularies, past summaries), retrieval-augmented answering

### Frontend
- All role-specific dashboards
- Report list + preview + export
- Notification preferences settings
- Document upload to AI knowledge base
- Citation UI in AI responses (which doc/patient row was the source)

### Testing
- Report accuracy: cross-check generated numbers against raw SQL
- **AI hallucination check**: rule = AI never fabricates numbers in reports
- RAG accuracy: 20-question eval set, ≥ 90% retrieval hit rate
- E2E: **Workflow 1 (Patient Journey) complete** — Registration → Appointment → Consultation → Billing → Payment

---

## Sprint 8 — RC: Observability + Hardening + Freeze (Nov 16 – Nov 29)

**Goal:** Everything measured. Everything tested. Ready to freeze.

### Backend
- `ai_assistant` Phase 3:
  - **AI Milestone 5: AI Observability** — `ai_interactions` table complete, token/cost tracking per hospital and per user, safety flags (PII detection, prompt-injection scanning), prompt version tracking, eval harness integration, budget enforcement with graceful degradation
- Performance tuning across the board
- Security hardening pass
  - Rate limiting on all public endpoints
  - CORS locked down
  - Header security review
  - Dependency vulnerability scan (zero critical)
- Backup and restore drill

### Frontend
- Polish pass across all screens
- Loading states, error boundaries, empty states
- Accessibility audit (WCAG 2.1 AA baseline)
- Mobile-responsive check on key screens
- Localization scaffolding (English only for v2.0, but hooks in place)

### Testing
- **All 4 E2E workflows passing**
- Load test: 500 concurrent users, 15-min sustained
- Chaos test: kill a backend replica mid-request, ensure graceful degradation
- Security review: OWASP Top 10 rechecked
- AI eval on full 50-scenario internal set: ≥ 90% helpful, < 2% hallucination
- Full regression run
- Bug bash (all-hands, 1 full day)

### RC Release Gate + Code Freeze (Nov 30)
✅ All success metrics for RC met (see roadmap):
- Pilot hospital environment ready
- API response < 500ms p95
- 500 concurrent users supported
- 99.9% uptime achievable
- AI response < 3s p95
- AI helpful ≥ 90%, hallucination < 2%
- All 4 E2E workflows passing
- 0 critical security issues
- < 5 P1 bugs, < 20 P2 bugs
- **Code freeze active from Nov 30 00:00 IST**

---

## Post-Freeze — Pilot Beta (Dec 1 – Dec 31)

**No new features. Only critical bug fixes.**

- **Week 1 (Dec 1 – 7):** Pilot hospital onboarding, staff training, environment setup
- **Week 2 (Dec 8 – 14):** Pilot goes live with real users, feedback loop
- **Week 3 (Dec 15 – 21):** Fix critical issues from pilot, load testing
- **Week 4 (Dec 22 – 31):** Deployment rehearsals, runbook validation, on-call schedule finalized

---

## Production Launch — January 2027

- Deploy to production
- 24/7 on-call rotation begins
- First live customer(s)
- Marketing announcement
- Weekly stability reviews for the first 8 weeks

---

## Pre-Agreed Cut List

**If we slip and have to cut scope, cut in this order** (top first). This is agreed by co-founders now so the decision isn't argued in the middle of Sprint 7 at 2am.

1. **AI-generated natural language dashboard summary** (defer to v2.1) — dashboards work without it
2. **RAG (AI Milestone 4)** (defer to v2.1) — Chat + Function Calling + Memory is still a strong AI story
3. **SMS notifications** — already deferred to v2.1
4. **AI slot recommendation in Appointments** — manual booking still works fine _(status 2026-10-06: already implemented; manual booking is unaffected when AI is off or fails)_
5. **Report PDF export** — CSV export still works, PDF can wait
6. **MFA (TOTP)** — password + lockout is still secure enough for pilot; MFA becomes v2.1

**Do not cut:**
- Multi-tenant isolation
- Audit logging
- API versioning
- Any security measure
- Any of the 4 E2E workflows
- Billing correctness (money must be right)

---

## Sprint Retrospective Template

At the end of each sprint retro, capture:

1. **Delivered:** what actually shipped vs planned
2. **Slipped:** what didn't ship and why
3. **Cut:** anything moved to the cut list
4. **Metrics:** velocity, bug count, test coverage, performance benchmarks
5. **Team:** blockers, morale, tooling issues
6. **Next sprint carryover:** items rolling forward

Log every retro to `docs/retros/sprint-N.md` and link from this file.

---

## Owner Assignments

_Fill this out at Monday's kickoff meeting. Every module needs a named owner._

| Module | Owner | Reviewer |
|--------|-------|----------|
| Authentication | _[TBD]_ | _[TBD]_ |
| User Management | _[TBD]_ | _[TBD]_ |
| Hospital Settings | _[TBD]_ | _[TBD]_ |
| Patient Management | _[TBD]_ | _[TBD]_ |
| Doctor Management | _[TBD]_ | _[TBD]_ |
| Appointment Management | _[TBD]_ | _[TBD]_ |
| Billing | _[TBD]_ | _[TBD]_ |
| AI Assistant | _[TBD]_ | _[TBD]_ |
| Reports & Dashboard | _[TBD]_ | _[TBD]_ |
| Notifications | _[TBD]_ | _[TBD]_ |
| Audit Logs | _[TBD]_ | _[TBD]_ |
| Frontend Design System | _[TBD]_ | _[TBD]_ |
| Infra / DevOps | _[TBD]_ | _[TBD]_ |
| QA / E2E | _[TBD]_ | _[TBD]_ |
