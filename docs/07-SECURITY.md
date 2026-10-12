# 07 — Security Architecture

Healthcare data is one of the most sensitive categories of data. Aetheris Health AI treats security as a first-class product requirement, not a compliance checklist.

---

## 1. Threat Model

Primary threats we defend against:

1. **Credential compromise** — phishing, weak passwords, reused passwords, token theft
2. **Insider misuse** — staff accessing records outside their role or need-to-know
3. **Data exfiltration** — bulk export, screenshots, SQL injection, IDOR
4. **Injection attacks** — SQL, prompt injection into AI, XSS in patient-facing fields
5. **Supply chain** — vulnerable dependencies, compromised provider SDKs
6. **Denial of service** — resource exhaustion, AI cost bombing
7. **Ransomware** — encryption of production data
8. **Multi-tenant leakage** — one hospital seeing another's data
9. **AI-specific** — prompt injection, jailbreaks, model output leaking training data or other tenants' data

## 2. Authentication

### 2.1 Password Policy

- Minimum 12 characters
- At least one uppercase, lowercase, digit, symbol
- Not present in known breach corpus (HaveIBeenPwned k-Anonymity API in v2.1)
- Cannot match previous 5 passwords
- Force change every 180 days for privileged roles
- No password hints, ever

### 2.2 Password Storage

- **Argon2id** via `passlib`
- Parameters tuned to ≥ 500ms cost on target hardware
- Bcrypt fallback for reads during algorithm migration only

### 2.3 Tokens

- **Access token:** JWT, RS256, 15 minutes, contains `user_id`, `hospital_id`, `roles`, `permissions`
- **Refresh token:** opaque UUID, 7 days, stored server-side in Redis, rotated on every refresh
- Refresh rotation with reuse detection: if a refresh token is used twice, all sessions for that user are invalidated
- Signing keys rotated every 90 days; JWKS endpoint published

### 2.4 Multi-Factor Authentication

- MVP: optional TOTP for Hospital Admin and Super Admin
- v2.1: mandatory for all admin roles
- Backup codes issued on enrollment
- No SMS-based MFA (SIM swap risk)

### 2.5 Account Lockout

- 5 failed attempts in 15 minutes → lockout for 30 minutes
- Progressive backoff (exponential) after repeated lockouts
- Admin can unlock earlier from the admin panel

## 3. Authorization

### 3.1 RBAC + Permissions

- Users have one or more **Roles**
- Roles map to **Permissions** (codes like `patient.read`, `billing.approve_discount`)
- Every endpoint requires a specific permission
- Permission checks happen at the API layer via a FastAPI dependency
- Row-level checks (e.g. doctor only sees their own patients) happen in the service layer

### 3.2 Predefined Roles

- Super Admin — platform-wide
- Hospital Admin — everything within one hospital
- Doctor — clinical, own patients
- Nurse — assigned patients, vitals, care coordination
- Receptionist — front desk operations
- Billing Staff — invoices, payments
- Lab Technician — lab orders, results
- Pharmacist — prescriptions, stock
- Inventory Manager — stock, purchase orders
- Patient — own record only (portal, future)

### 3.3 Least Privilege

- Default new role = zero permissions
- Bulk permission grants require Hospital Admin approval workflow
- Emergency access ("break glass") is logged, notified, and time-limited (v2.2)

## 4. Multi-Tenant Isolation

- Every query filters by `hospital_id` from the authenticated user's context
- Enforced in the repository base class — impossible to write a repository method that skips it
- v2.2: PostgreSQL Row-Level Security policies as a second line of defense
- Super Admin traffic is separately audited and requires MFA

## 5. Data Protection

### 5.1 In Transit

- TLS 1.2+ everywhere (staging and production)
- HSTS enabled with a 1-year max-age
- Internal service-to-service traffic also TLS (when we split services)

### 5.2 At Rest

- Database at rest encryption enabled (managed provider or LUKS)
- Object storage server-side encryption (SSE-KMS)
- Field-level encryption for MRN, phone, address (v2.2), using envelope encryption with a KMS
- Backup encryption with rotated keys

### 5.3 Secrets

- Only via environment variables in MVP
- v2.2: HashiCorp Vault or equivalent
- No secrets in git — enforced by `git-secrets` in pre-commit
- No secrets in logs, ever
- Secret rotation policy documented per secret category

## 6. Input Handling

- All input validated at the API boundary via Pydantic
- Every string field has a max length
- Every file upload validated: mime, size, extension
- Never build SQL by concatenation — SQLAlchemy or parameterized queries only
- HTML in patient-facing rich text fields sanitized with `bleach`
- Redirects to user-supplied URLs are whitelisted

## 7. AI-Specific Security

Prompt injection is a real threat when clinical data flows through LLMs. Our defenses:

### 7.1 Input Isolation

- User content (patient notes, uploaded files) is delimited in prompts using structural markers
- We never let user content instruct the model to change behavior; system prompts are cryptographically fenced
- Prompt templates are versioned and reviewed like code

### 7.2 Output Constraints

- AI outputs used to trigger actions go through **function calling with typed schemas**, not free text
- Function results are validated against Pydantic schemas before service invocation
- No "eval this SQL" style tools, ever
- No tool that can escalate the caller's permissions

### 7.3 Data Boundaries

- AI provider requests include **only** data the calling user is authorized to see
- AI can request more data only through **service-mediated tool calls**, which re-check permissions
- Vector store (RAG) is per-hospital, isolated at query time
- No cross-tenant retrieval, ever

### 7.4 PII Minimization

- Prompts strip identifiers where not needed (e.g. summarization tasks receive DOB replaced with age)
- Provider agreements reviewed for training data usage; providers that train on our data are excluded
- Logging of prompts/completions is opt-in with retention policy

### 7.5 Rate & Cost Controls

- Per-user, per-hospital, per-endpoint AI budgets
- Budget exceeded → 429 with clear message; admin can raise limits
- Prevents "AI cost bomb" abuse

> **Status, 2026-10-06:** not built. What limits AI use today is the per-user request rate limit (`RATE_LIMIT_AI_PER_MIN`), a cap on concurrent model calls per process (`AI_MAX_CONCURRENT_CALLS`) and a deadline per call. There is no per-hospital budget or cost ceiling yet.

## 8. Session Security

- Session TTL: 12 hours idle, absolute 7 days (matches refresh)
- Concurrent session cap per role (configurable)
- Session revocation surfaces:
  - Self-service: "Logout all devices"
  - Admin: "Terminate user sessions"
  - Automatic: password change, role change, suspicious activity flag

## 9. Audit Logging

Every event that touches sensitive data is logged. Non-negotiable events:

- Login, login failure, logout
- User created, updated, role changed
- Patient record viewed (read logging in v2.2)
- Patient record created / updated
- Medical record accessed
- Invoice created / voided / refunded
- Payment recorded
- Prescription dispensed
- Data exported
- Permission changed
- AI interaction — today one structured `ai_interaction` log line per model call; the `ai_interactions` table is planned and not built
- Impersonation used (admin acting as user, future)

Audit logs are:
- Immutable (no `UPDATE`, no `DELETE`)
- Segregated from application logs
- Reviewed by Hospital Admin dashboards
- Retained per compliance policy (default 7 years)

## 10. Application-Level Protections

- **CSRF:** JWT in `Authorization` header (not cookies) → CSRF-immune for API. If we introduce cookies, we add CSRF tokens.
- **XSS:** React auto-escapes; content sanitization on inbound; CSP headers.
- **Clickjacking:** `X-Frame-Options: DENY`, `Content-Security-Policy: frame-ancestors 'none'`
- **CORS:** whitelisted origins per environment
- **CSP:** strict — no inline scripts, no `unsafe-eval`
- **HSTS:** `max-age=31536000; includeSubDomains; preload`
- **Referrer Policy:** `no-referrer`
- **Permissions Policy:** deny geolocation, camera, microphone by default

## 11. Dependency Security

- `pip-audit` and `npm audit` in CI, blocking on critical
- Dependabot (or Renovate) for automatic PRs
- Manual review of AI provider SDK updates (they touch data in flight)
- Pinned major versions; regularly reviewed

## 12. File Uploads

- Mime and extension validated server-side (never trust the client)
- Max file size per endpoint
- Files stored **outside** the web root
- Signed URLs for download with short TTL
- Anti-virus scan in v2.1 (ClamAV or hosted API)

## 13. Database Security

- Application user has the minimum privileges required (no `DROP`, no `CREATE ROLE`)
- Migrations run as a separate DB user with elevated privileges — used only for migration runs
- Read replicas serve read-heavy analytics workloads
- No production data in dev; realistic-but-synthetic data via a data factory

## 14. Backups & Recovery

- Encrypted daily backups + continuous WAL streaming
- Restore drills quarterly; documented RPO ≤ 15 minutes, RTO ≤ 2 hours for MVP
- Backups stored in a separate cloud account/region
- Ransomware defense: object storage immutability (WORM) for backup archives

## 15. Incident Response

- On-call rotation once we have paying hospitals
- Runbook documented for common incidents (see `docs/incidents/` in v2.1)
- Breach notification policy per regulation
- Post-mortem template; blameless; all incidents result in a doc-level change or a preventive engineering task

## 16. Compliance Posture

- **India:** DPDP Act 2023 — consent, purpose limitation, data localization considered
- **US-adjacent:** HIPAA-equivalent controls baked in for future US expansion (encryption, audit, access controls, BAA-ready posture)
- **EU (future):** GDPR data subject rights (export, delete) — data export API in v2.2

We do not claim certifications we don't have. When we're audit-ready, we say so.

## 17. Developer Security Hygiene

- No production credentials on developer machines
- SSH keys required for repo access; passwordless auth disabled on prod
- 2FA mandatory on GitHub, cloud provider, Anthropic/OpenAI dashboards
- Secrets provisioning through a break-glass process for on-call engineers only
- Every commit signed (GPG or SSH signing) — enforced by main branch protection

## 18. Reporting Vulnerabilities

- `security@aetheris.health` (once mailbox exists)
- Bug bounty program from v2.2 onwards
- Responsible disclosure policy publicly posted

## 19. Non-Negotiables

The following rules never bend:

1. No plaintext passwords, ever
2. No secrets in code or logs, ever
3. No cross-tenant data access, ever
4. No SQL string concatenation, ever
5. No AI action without service-layer validation, ever
6. No unaudited change to permissions, ever
7. No production access without MFA, ever
8. No customer data in developer environments, ever
9. No untested code path for authentication or authorization, ever
10. No shipping a feature that violates any of the above, ever

If a proposed feature can't ship while respecting these, we don't ship it.
