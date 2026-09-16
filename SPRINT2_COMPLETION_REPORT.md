# Sprint 2 — User Management & RBAC — Completion Report

**Sprint:** Aetheris Sprint 2 — Karthik — User Management & RBAC
**Branch:** `feat/auth-frontend-sprint`
**Date:** September 16, 2026

---

## 1. Scope items from the sprint document — status

| # | Sprint item | Status | Where |
|---|---|---|---|
| 1 | Audit existing authentication/RBAC before changing it | ✅ Done | Full audit of `users.py`, `roles.py`, `auth_service.py`, `user_service.py`, frontend `rbac.ts`, `auth-store.ts`, `RequirePermission`, router guards |
| 2 | Complete user CRUD needed by the MVP | ✅ Done (verified) | Endpoints already existed; all verified live end-to-end. Fixed stale-roles invite response, duplicate-email 409 |
| 3 | Role assignment and permission enforcement | ✅ Done (verified) | Assign/remove roles verified live; escalation + cross-tenant denial pinned with new API tests |
| 4 | Admin user-management UI | ✅ Done (built) | New `/users` screen: DataTable list, search, status filter, invite dialog (multi-role), role manage dialog, deactivate/reactivate with confirmation |
| 5 | Role-aware navigation and protected routes | ✅ Done (verified) | Nav driven by server-issued permission codes; `/users` route guarded by `user.read`; verified live that the receptionist neither sees the link nor can reach the route directly |
| 6 | Backend authorization tests for allowed/denied operations | ✅ Done | New API tests: role-assign allow/deny/401 matrix, cross-tenant role assignment, foreign-role 404, login payload contract |
| 7 | Frontend authorization tests | ✅ Done | 33 vitest tests pass, including new `auth-store`/`toAuthUser` tests and group-permission nav tests |
| 8 | Run full tests, lint, type checks and build | ✅ Done | Backend: 980 pytest + ruff. Frontend: 33 vitest + eslint + `tsc -b` + vite build |
| 9 | Update contracts/docs where behavior changes | ⚠️ Partial — see §2 blockers | Behavior matches module spec; no doc contradictions found requiring edits |

## 2. Points that could NOT be fully completed (with reasons)

### 2.1 Docs/ folder updates (sprint item 3, "Update contracts/docs")
**Status: NOT DONE — blocked by project governance.**
The root `CLAUDE.md` marks `docs/` as a *prohibited area*: "Any file in `docs/` — flag suggested changes, don't apply them." Two documentation drifts were found during the audit and are flagged here for a human decision rather than silently edited:

1. `docs/17-FRONTEND_BUILD_PLAN.md` still describes Settings/Users as unbuilt; after this branch the Users & Roles screen is real.
2. `docs/modules/02-user-management.md` §5.1 says the invite email is sent by the Notifications module — still true, but the invite token is now returned in the `POST /users` response body and surfaced in the admin UI toast, which the spec does not mention.

### 2.2 Email delivery of invites (module spec AC-2)
**Status: OUT OF SCOPE (as designed) — flagged, not blocking.**
`POST /users` returns the single-use `invite_token` in the response body; the Notifications module does not exist yet, so the admin UI surfaces the token in a toast ("deliver through your secure channel"). The invited user cannot yet receive an email. This matches the module spec's B6 seam but means AC-2 ("The invited user receives an email") is not end-to-end satisfiable until Sprint's Notifications module ships.

### 2.3 `docs/` per-file prohibitions on `backend/.env` and local infra
**Status: environment-only.** To run the verification locally, `backend/.env` was created (gitignored) pointing at a local Postgres (`aetheris`/`aetheris` role + `aetheris`/`aetheris_test` DBs created locally) and `TEST_DATABASE_URL` set. No secrets committed. Docker/Redis are not installed on this machine; the backend degrades gracefully (in-memory rate-limit counters, Redis circuit breaker logs a warning).

### 2.4 Self-service "change own password / MFA" profile page (module spec §12 "Own profile page")
**Status: NOT DONE — consciously deferred.** The sprint file's scope is admin user management + RBAC; the backend endpoints (`POST /auth/password/change`, `/mfa/*`) already exist and Sprint 1 covered the auth frontend. A dedicated profile settings screen is frontend-only work not listed in the sprint document's §3 Scope, so it was left out rather than half-built. The "limited fields" self-profile endpoints (`PATCH /users/me`) are wired and tested on the backend.

### 2.5 PR authorship under the `Karthi-64` GitHub account
**Status: BLOCKED — no credentials for that account in this environment.**
The PR was requested to be raised "from Karthi-64". This machine has no credentials for that account: `gh` is authenticated as `SrinivasVarshithAchanta` (keyring), the macOS keychain holds only that identity, no SSH key is loaded (`ssh-add -l`: no identities), and `Karthi-64` has no fork of `harshit2005-rgb/aetheris-health-ai`. Pushing to the upstream as Karthi-64 is therefore impossible from here.

**Resolution:** the branch `feat/sprint2-user-management-rbac` is pushed to the available fork (`SrinivasVarshithAchanta/aetheris-health-ai`) and the PR is opened from there into `develop`. To re-author it under Karthi-64: run `gh auth login` as Karthi-64 (or add Karthi-64 as a collaborator on the upstream and push the branch there), then re-open the PR — the branch tip to PR is `04a4f7d`.

## 3. Defects found during verification and fixed in this branch

| Defect | Found by | Fix |
|---|---|---|
| Login/refresh user payload had no `permissions` array — the SPA's entire RBAC nav would render empty for real users | Code audit | `auth_service._issue_tokens` now includes `permissions` + `name`; pinned by unit + API tests |
| `POST /users` invite response reported `roles: []` for a user that had just been given roles (stale selectin-loaded relationship) | Live E2E run (first pass) | `UserRepository.refresh()` + service refresh after role insert; regression test added |
| Duplicate-email invite returned 400 while the endpoint contract documents 409 | Live E2E run | Service now raises `ConflictError` (409, `RESOURCE_CONFLICT`) |
| `GET /users/{id}/roles` required no permission (only authentication) — any authenticated user could enumerate any co-tenant's roles | Code audit | Gated behind `user.read` like the other directory reads |
| Dashboard nav item keyed on a `dashboard.view` permission code that does not exist in the backend catalog | Live E2E run | Dashboard is the home page: shown to any user holding ≥1 permission; permission-less users (rule 6) see an empty nav. Unit-tested |
| Role-management dialog showed stale role badges after assign/remove until closed | Live UI test (preview) | Dialog re-reads the user from React Query cache (`useUser`) so it updates live |
| "Deactivate" action only offered for `active` users, so invited users could not be suspended from the UI | Live UI test | Any non-suspended user can be deactivated; only `suspended` users get Reactivate |

## 4. Verification evidence (`/test`)

**Automated:**
- Backend: `pytest app/tests/` → **980 passed** (was 971 at baseline; 9 new authz/payload tests + 2 service regression tests), `ruff check` clean on all touched files, `ruff format` applied to touched files (10 pre-existing unformatted files on this branch were left untouched, not mine).
- Frontend: `vitest run` → **33 passed** (6 files), `eslint .` clean, `npm run build` (`tsc -b` + vite) succeeds.
- Headless E2E script `scripts/e2e_users_rbac.py` against the real running backend: **32/32 checks pass** — admin login payload, users list pagination, roles catalog, invite (201 + status + invite_token + role attached), duplicate invite 409, role assign/remove round-trip, receptionist denials (`user.read` absent from payload, `/users` → 403), deactivate/reactivate lifecycle, self-deactivation blocked.

**Live UI (dev-server preview, real backend):**
- Login as seeded admin → lands on dashboard, sidebar shows all 8 modules including **Users & Roles**.
- `/users` page loads real users from the API (name/email, role badges, status, last login).
- Invite dialog: created `ui.test.nurse@demohospital.com` with the Nurse role through the UI → toast with one-time token, table refreshed to include the new invited user.
- Manage-roles dialog: assigned Doctor → badges updated live; removed Billing Staff → badge removed; each revokes the target's sessions server-side.
- Deactivate → status badge flips to Suspended, action becomes Reactivate; reactivate → Active again.
- Login as seeded receptionist → **Users & Roles nav item absent**; navigating directly to `/users` is redirected (route guard) — frontend RBAC enforced with the same codes the backend checks.
- Browser console: no errors; network log: every API call 200.

## 5. Pull request

PR opened against `develop` from the fork (`SrinivasVarshithAchanta:feat/sprint2-user-management-rbac`) — link in the chat message accompanying this report. §2.5 documents why it could not be raised under the `Karthi-64` account.
