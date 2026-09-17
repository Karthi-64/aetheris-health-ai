# 02 — User & Role Management

**Owner:** TBD
**Phase:** MVP
**Status:** Approved

---

## 1. Purpose

Manage the humans (and future service accounts) who log into Aetheris. Own user identity, role assignments, permission grants, and the lifecycle from invitation through deactivation. Every module reads user context; this module writes it.

## 2. Scope

### In Scope

- Create, view, update, deactivate users
- User profile fields (name, contact, employment)
- Assign one or more roles per user
- Predefined system roles (10 total, seeded)
- Custom role creation (v2.2) — MVP has read-only for system roles
- Permissions catalog (read-only, seeded)
- Bulk user import from CSV (v2.1)
- Directory search across staff

### Out of Scope

- Authentication (credentials, sessions) → `01-authentication.md`
- Patient user accounts (patient portal) → future
- Doctor-specific fields (specialization, license) → `04-doctor-management.md`

## 3. Personas & Permissions

| Role | Can do |
|---|---|
| Super Admin | Everything, across all hospitals |
| Hospital Admin | Create/edit/deactivate users in their hospital; assign roles from the system role list |
| Anyone authenticated | View own profile; update own basic profile fields |
| Any role viewer | View users in their hospital (read-only) if granted `user.read` |

## 4. Business Rules

1. A user belongs to exactly one hospital (except Super Admin, whose `hospital_id` is NULL).
2. Email is unique per hospital (case-insensitive).
3. A user can hold multiple roles; permissions are the union.
4. System roles cannot be deleted; they can be renamed only if `is_system = false`.
5. Deactivating a user preserves history; deletion is soft delete only.
6. Removing all roles from a user forces them into a "no access" state — they can log in but see nothing.
7. Users cannot deactivate themselves.
8. Users cannot grant themselves permissions they don't already have (no privilege escalation).
9. Assigning a role writes an entry to `user_roles` with `assigned_by` and `assigned_at`.

## 5. Workflow

### 5.1 Invite user

1. Admin fills out `POST /users` with email, name, roles.
2. Service creates user with `status = 'invited'`, `password_hash = NULL`, `password_change_required = true`.
3. Notification service emails an invitation link with a one-time setup token (reuses password reset token infrastructure).
4. User clicks link, sets password → status becomes `active`.
5. Audit: `user.invited`, later `user.activated`.

> **Interim (MVP):** the Notifications module does not exist yet, so step 3
> does not happen. `POST /users` returns the raw one-time token as
> `invite_token` in the response body — the only time it is ever readable —
> and the admin UI surfaces it for delivery through an out-of-band channel.
> AC-2 is therefore not end-to-end satisfiable until Notifications ships;
> the token itself, its single use and its TTL already work.

### 5.2 Deactivate

1. Admin calls `POST /users/{id}/deactivate`.
2. Service sets `status = 'suspended'`, revokes all refresh tokens (via Auth service).
3. Audit: `user.suspended`.

### 5.3 Assign role

1. Admin calls `POST /users/{id}/roles` with role id.
2. Service checks admin has all permissions the role includes (prevents escalation via assignment).
3. Adds `user_roles` row, recording `assigned_by` and the assignment time.
4. Revokes user's refresh tokens (forces reissue with new claims).
5. Audit: `user.role_assigned`.

> The same check guards `POST /users`: an invite grants permissions exactly as
> an assignment does, and the caller receives the invite token, so an
> unguarded invite is the shorter path to the same escalation.
>
> **Consequence for the seeded catalog:** because an admin may only grant what
> they already hold, the Hospital Admin role has to remain a superset of every
> hospital-scoped role, or it silently loses the ability to staff those roles.
> Adding a permission to a clinical role means adding it to Hospital Admin too.

### 5.4 Directory search

1. Any authenticated user calls `GET /users?q=<term>&role=doctor`.
2. Service filters by `hospital_id` from context.
3. Returns paginated list.

## 6. Functional Requirements

- FR-1: The system shall allow admins to create users with roles.
- FR-2: The system shall enforce unique email per hospital.
- FR-3: The system shall prevent privilege escalation via role assignment.
- FR-4: The system shall support soft delete only.
- FR-5: The system shall provide directory search across staff.
- FR-6: The system shall seed permissions and system roles.

## 7. Non-Functional Requirements

- User list p95 < 300ms with 10,000 users.
- Directory search returns first page in < 500ms.
- All user mutations audited.

## 8. Database Design

Tables defined in `05-DATABASE_DESIGN.md`: `users`, `roles`, `permissions`, `role_permissions`, `user_roles`.

Indexes:
- `uq_users_hospital_email (hospital_id, LOWER(email))`
- `ix_users_status (hospital_id, status)`

## 9. API Design

```
GET    /api/v1/users/me
PATCH  /api/v1/users/me                 # limited fields (name, phone)
GET    /api/v1/users                    # filter: role, status, q; paginated
POST   /api/v1/users                    # invite
GET    /api/v1/users/{id}
PATCH  /api/v1/users/{id}
POST   /api/v1/users/{id}/deactivate
POST   /api/v1/users/{id}/reactivate
POST   /api/v1/users/{id}/reset-password  # admin trigger
GET    /api/v1/users/{id}/roles
POST   /api/v1/users/{id}/roles
DELETE /api/v1/users/{id}/roles/{role_id}
GET    /api/v1/roles
GET    /api/v1/roles/{id}
GET    /api/v1/permissions               # read-only catalog
```

## 10. Permissions

- `user.read`
- `user.create`
- `user.update`
- `user.deactivate`
- `user.reset_password`
- `role.read`
- `role.assign`
- `role.create` (v2.2)
- `role.update` (v2.2)
- `role.delete` (v2.2)

## 11. Validation Rules

- Name: 1–100 chars.
- Email: RFC 5322, lowercased for storage.
- Phone: E.164 format, optional.
- Role IDs: must exist and belong to the user's hospital (or be system roles).

## 12. UI Requirements

- Users list with filters (role, status), search, columns for name / email / roles / last login / status.
- User invite modal (email, name, roles).
- User detail with tabs: Profile, Roles, Sessions (v2.1), Audit (v2.1).
- Own profile page: change name, phone, password, MFA settings.

## 13. AI Integration Points

- v2.2: AI-assisted onboarding — suggest role based on job title description entered by admin. Uses `user.suggest_role` prompt; output is a suggestion only; admin confirms.

## 14. Edge Cases

- Admin deletes themselves → blocked with 400 `BUSINESS_RULE_VIOLATION`.
- Last remaining Hospital Admin tries to remove admin role → blocked with 400 `BUSINESS_RULE_VIOLATION`. Keyed on the capability, not the role name: the check refuses when no *other active* user in the hospital would still hold `role.assign`.
- Bulk import: partial failures return per-row status; no partial commit.
- Case-sensitivity in email: enforced case-insensitive by index.

## 15. Cross-Module Dependencies

- Depends on: Auth (for token revocation on role change / password reset).
- Provides to: every module (user context, role checks, actor for audit).

## 16. Testing Requirements

- Unit: privilege escalation prevention logic.
- Repository: unique email constraint.
- API: full CRUD happy path + permission denied cases.
- Integration: invite → activation → login → role change → forced re-login.

## 17. Acceptance Criteria

- AC-1: A Hospital Admin can invite a user with roles in under 30 seconds.
- AC-2: The invited user receives an email and can set their password. *(Blocked on the Notifications module — see §5.1. The token half works today.)*
- AC-3: An admin cannot assign a role containing permissions they lack.
- AC-4: Deactivating a user immediately terminates their sessions.
- AC-5: The permissions catalog is seeded and read-only in MVP.

## 18. Rollout Plan

- Ships with MVP.
- Data seed on first deployment: permissions catalog + system roles + role→permission mapping.

## 19. Future Scope

- Custom role creation (v2.2)
- Bulk import (v2.1)
- Session inspection (v2.1)
- Delegated administration (v2.3)
- Just-in-time access grants (v3)

## 20. Open Questions

- None currently.
