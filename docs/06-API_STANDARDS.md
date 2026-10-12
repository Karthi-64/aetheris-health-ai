# 06 — API Design Standards

Every REST endpoint in Aetheris Health AI follows these conventions. No exceptions.

---

## 1. Style

- **REST-first**, resource-oriented
- **JSON** request and response bodies
- **UTF-8** encoding
- **HTTPS-only** in staging and production

## 2. Versioning

- URL-based versioning: `/api/v1/...`
- Breaking changes bump the major version. Additive changes do not.
- We support at most **two concurrent versions** in production
- Deprecation window: 6 months minimum

Examples:
- `GET /api/v1/patients`
- `POST /api/v1/appointments`

## 3. Resource Naming

- Plural nouns: `/patients`, `/appointments`, `/invoices`
- Never verbs: no `/createPatient`, no `/getAppointment`
- Nested resources when the relationship is genuine and the child is only meaningful in context of the parent:
  - `/patients/{id}/consultations`
  - `/appointments/{id}/status-history`
- Actions that don't fit CRUD get a sub-resource:
  - `POST /appointments/{id}/check-in`
  - `POST /invoices/{id}/void`
  - `POST /consultations/{id}/finalize`

## 4. HTTP Methods

| Method | Purpose |
|---|---|
| GET | Retrieve a resource or collection. Never has side effects. |
| POST | Create a resource, or perform an action that has side effects. |
| PUT | Replace an entire resource. |
| PATCH | Partial update. |
| DELETE | Soft delete by default. Permanent deletion only for expressly non-medical resources. |

## 5. Standard Response Envelope

Every response is wrapped in a consistent envelope. This is what SDKs and the frontend contract against.

### 5.1 Success

```json
{
  "success": true,
  "message": "Patient created successfully.",
  "data": {
    "id": "3f6c...",
    "mrn": "MRN-2026-00042",
    "first_name": "Ananya",
    "last_name": "Rao"
  },
  "metadata": {
    "request_id": "b6e2..."
  }
}
```

### 5.2 List Success

```json
{
  "success": true,
  "message": "Patients retrieved.",
  "data": [ { "...": "..." } ],
  "metadata": {
    "request_id": "b6e2...",
    "pagination": {
      "page": 1,
      "page_size": 25,
      "total_records": 137,
      "total_pages": 6
    }
  }
}
```

### 5.3 Failure

```json
{
  "success": false,
  "message": "Validation failed.",
  "errors": [
    { "field": "email", "message": "Invalid email format" },
    { "field": "date_of_birth", "message": "Cannot be in the future" }
  ],
  "error_code": "VALIDATION_ERROR",
  "metadata": {
    "request_id": "b6e2..."
  }
}
```

### 5.4 `error_code` catalog

Documented in `app/core/error_codes.py`. A few examples:

| Code | HTTP | Meaning |
|---|---|---|
| `VALIDATION_ERROR` | 422 | Input validation failed |
| `AUTHENTICATION_REQUIRED` | 401 | Missing / invalid token |
| `PERMISSION_DENIED` | 403 | Authenticated but not authorized |
| `RESOURCE_NOT_FOUND` | 404 | Target does not exist |
| `RESOURCE_CONFLICT` | 409 | e.g. duplicate MRN |
| `BUSINESS_RULE_VIOLATION` | 400 | e.g. cannot book past appointment |
| `RATE_LIMITED` | 429 | Too many requests |
| `FEATURE_DISABLED` | 403 | The hospital has not been given this feature (its feature flag is not on) |
| `AI_NOT_CONFIGURED` | 503 | AI is not configured on this server: no provider key, or the AI kill switch is off |
| `AI_PROVIDER_UNAVAILABLE` | 503 | The AI provider could not be used: unreachable, or it answered with an error |
| `AI_PROVIDER_TIMEOUT` | 503 | The AI provider did not answer within the deadline |
| `AI_RESPONSE_INVALID` | 503 | The AI provider answered, but the server rejected the answer |
| `INTERNAL_ERROR` | 500 | Unhandled |

**AI failure codes.** All four `AI_*` codes are HTTP 503, so a client must branch on
`error_code`, not on the status. Today they are returned by one endpoint,
`POST /api/v1/appointments/recommend-slot`; `FEATURE_DISABLED` is returned there too. The
`message` of each is fixed text and `errors` is `null` — nothing from the provider, the
prompt or the model's answer is put in a response:

| Code | `message` |
|---|---|
| `AI_NOT_CONFIGURED` | `AI suggestions are not configured on this server.` |
| `AI_PROVIDER_UNAVAILABLE` | `The AI service is unavailable right now. Choose a slot manually.` |
| `AI_PROVIDER_TIMEOUT` | `The AI service took too long to respond. Choose a slot manually.` |
| `AI_RESPONSE_INVALID` | `The AI suggestion could not be used. Choose a slot manually or try again.` |

There is one provider and one attempt per request — no retry and no fallback provider —
so `AI_PROVIDER_UNAVAILABLE` means that single call failed, not that several providers
were tried. It also covers a provider-side error such as a rejected key, the provider's
own rate limit or a model it no longer serves, and this server's cap on concurrent AI
calls. `AI_RESPONSE_INVALID` covers an answer that was truncated, not valid JSON, the
wrong shape or outside the options the server offered, and a suggestion that failed the
server's re-check against the database. An AI failure never blocks the non-AI path: the
user completes the task by hand. The full contract is in
[18-API_CONTRACTS.md](18-API_CONTRACTS.md) §5.6.

## 6. HTTP Status Codes

| Code | When |
|---|---|
| 200 OK | Successful GET / PATCH / PUT |
| 201 Created | Successful POST that created a resource |
| 202 Accepted | Async job queued |
| 204 No Content | Successful DELETE, empty response |
| 400 Bad Request | Business rule violation |
| 401 Unauthorized | No / invalid credentials |
| 403 Forbidden | Authenticated, not authorized |
| 404 Not Found | Resource missing |
| 409 Conflict | Concurrent modification, unique violation |
| 422 Unprocessable Entity | Validation failure |
| 429 Too Many Requests | Rate limited |
| 500 Internal Server Error | Unhandled |
| 503 Service Unavailable | Dependency down |

## 7. Authentication

- All authenticated endpoints require `Authorization: Bearer <access_token>`
- Access token: 15 minutes, JWT signed with rotating key
- Refresh token: 7 days, opaque, stored server-side, rotated on use
- Refresh endpoint: `POST /api/v1/auth/refresh`
- Logout endpoint: `POST /api/v1/auth/logout` (invalidates refresh)
- Logout-all endpoint: `POST /api/v1/auth/logout-all` (invalidates all refresh tokens for user)

## 8. Authorization

- Every endpoint declares its required permission in the FastAPI dependency
- Example: `Depends(require_permission("patient.create"))`
- Permission missing → 403 with `PERMISSION_DENIED`
- Row-level checks (e.g. doctor can only view their patients) happen in the service layer

## 9. Pagination

Query params:
- `page` (default 1, minimum 1)
- `page_size` (default 25, max 100)

Response `metadata.pagination` shown in section 5.2.

For very large datasets (audit logs; AI interactions once they are stored — planned, there is no such list today), use cursor pagination:
- Query params: `cursor`, `page_size`
- Response: `metadata.pagination.next_cursor`

## 10. Filtering

Query params match column names, with operator suffixes when needed:

- `?status=booked`
- `?scheduled_start_gte=2026-08-01T00:00:00Z&scheduled_start_lt=2026-08-02T00:00:00Z`
- `?doctor_id=<uuid>`
- `?q=rao` (full-text search on searchable fields)

Operator suffixes: `_gt`, `_gte`, `_lt`, `_lte`, `_in` (comma-separated), `_ne`.

## 11. Sorting

- `?sort=scheduled_start` — ascending
- `?sort=-scheduled_start` — descending
- Multi-sort: `?sort=-scheduled_start,doctor_id`

## 12. Idempotency

Endpoints that create financial records or trigger external side effects support idempotency via header:

```
Idempotency-Key: <client-generated-uuid>
```

Server stores `(idempotency_key, hospital_id, endpoint) → response` for 24 hours. Repeated requests get the cached response.

Required for: `POST /invoices/{id}/payments`, `POST /appointments`, `POST /prescriptions`.

## 13. Content Negotiation

- Requests: `Content-Type: application/json`
- Responses: `Content-Type: application/json` unless the endpoint explicitly returns a file (PDF, CSV)
- File uploads: `multipart/form-data`
- Streaming AI responses: `text/event-stream` (SSE) or `application/x-ndjson` — **planned**; no endpoint streams today (§20)

## 14. CORS

- Allowed origins per environment via config
- MVP: hospital-specific origins whitelisted
- Never `*` in production

## 15. Rate Limiting

Applied at the middleware level, tracked in Redis:

- Anonymous: 60 requests/minute per IP
- Authenticated: 300 requests/minute per user, 1000/minute per hospital
- AI endpoints: a separate lower limit (cost control) — 30 requests/minute per user by default. Today that is `POST /appointments/recommend-slot`; the hospital limit applies to it as well

Response headers:
```
X-RateLimit-Limit: 300
X-RateLimit-Remaining: 287
X-RateLimit-Reset: 1735920000
```

## 16. Request IDs

Every request gets a UUID `X-Request-ID`. Generated by the middleware if absent. Present in:
- Response header `X-Request-ID`
- Response envelope `metadata.request_id`
- Structured logs
- Audit log entries

Makes debugging trivial.

## 17. Time Handling

- **Wire format:** ISO 8601 with timezone (`2026-08-15T09:30:00+05:30` or `Z`)
- **Storage:** UTC
- **Display:** user's hospital timezone
- No naive datetimes cross the API boundary. Ever.

## 18. Money Handling

- Represented as decimal strings in JSON: `"1499.00"` (not float)
- Always include currency code alongside: `"currency": "INR"`
- Never round in the API; the service layer decides rounding rules

## 19. File Uploads

- Uploaded via `multipart/form-data` to a dedicated endpoint (e.g. `POST /patients/{id}/documents`)
- Server validates: mime type, size (default max 10 MB), virus scan (v2.1)
- Stored in S3-compatible object storage
- Response includes a short-lived signed URL for download

## 20. Streaming Endpoints

> **Planned — not built.** No endpoint streams today and `POST /ai/summarize` does not
> exist. The one AI endpoint, `POST /appointments/recommend-slot`, returns an ordinary
> JSON envelope. The convention below is what a streaming endpoint must follow when one
> is added.

AI endpoints that produce large or long responses stream:

- `POST /ai/summarize` returns SSE stream by default
- Non-streaming fallback via `?stream=false`

Each SSE event:
```
event: token
data: {"delta": "The patient..."}

event: done
data: {"total_tokens": 342, "cost_usd": "0.00081"}
```

## 21. Deprecation

- Deprecated endpoints return response header `Deprecation: true` and `Sunset: <date>`
- Deprecation is announced in `docs/CHANGELOG.md` and to hospital admins via notification
- Minimum 6-month sunset window

## 22. Documentation

- FastAPI auto-generates OpenAPI at `/openapi.json`
- Swagger UI at `/docs`, ReDoc at `/redoc` — both **disabled in production** or protected by SSO
- Every endpoint has:
  - Summary
  - Description
  - `response_model`
  - `responses={401: ..., 403: ..., 422: ...}` documented
  - Examples where non-trivial

## 23. Backwards Compatibility Within a Version

Within `v1`:
- We can add optional fields to requests
- We can add fields to responses
- We cannot remove or rename fields
- We cannot change field types
- We cannot make required fields required-with-a-new-meaning
- We cannot change status codes for existing scenarios

Anything that violates these ships in `v2`.

## 24. Testing Requirements

Every endpoint has:
- Happy path test
- One test per permission-denied case
- Validation failure test
- Not-found test (where applicable)
- Idempotency test (where applicable)

---

## 25. Reference Endpoint Set (MVP)

Not exhaustive — module specs are the source of truth — but a taste:

```
POST   /api/v1/auth/login
POST   /api/v1/auth/refresh
POST   /api/v1/auth/logout
POST   /api/v1/auth/logout-all
POST   /api/v1/auth/password/forgot
POST   /api/v1/auth/password/reset

GET    /api/v1/users/me
GET    /api/v1/users
POST   /api/v1/users
GET    /api/v1/users/{id}
PATCH  /api/v1/users/{id}
DELETE /api/v1/users/{id}

GET    /api/v1/patients
POST   /api/v1/patients
GET    /api/v1/patients/{id}
PATCH  /api/v1/patients/{id}
GET    /api/v1/patients/{id}/summary       # AI-generated — planned, not built
GET    /api/v1/patients/{id}/consultations
POST   /api/v1/patients/{id}/documents

GET    /api/v1/doctors
POST   /api/v1/doctors
GET    /api/v1/doctors/{id}
PATCH  /api/v1/doctors/{id}
GET    /api/v1/doctors/{id}/availability
GET    /api/v1/doctors/{id}/slots?date=YYYY-MM-DD

GET    /api/v1/appointments
POST   /api/v1/appointments
GET    /api/v1/appointments/{id}
PATCH  /api/v1/appointments/{id}
POST   /api/v1/appointments/{id}/check-in
POST   /api/v1/appointments/{id}/start
POST   /api/v1/appointments/{id}/complete
POST   /api/v1/appointments/{id}/cancel
POST   /api/v1/appointments/{id}/no-show
POST   /api/v1/appointments/recommend-slot  # optional AI suggestion of one free slot; advisory, books nothing

GET    /api/v1/hospitals/current/feature-flags  # which gated features the caller's hospital can use

GET    /api/v1/services
POST   /api/v1/services
PATCH  /api/v1/services/{id}

GET    /api/v1/invoices
POST   /api/v1/invoices
GET    /api/v1/invoices/{id}
PATCH  /api/v1/invoices/{id}
POST   /api/v1/invoices/{id}/issue
POST   /api/v1/invoices/{id}/void
POST   /api/v1/invoices/{id}/payments

GET    /api/v1/notifications
POST   /api/v1/notifications/{id}/read
POST   /api/v1/notifications/read-all

GET    /api/v1/audit-logs
GET    /api/v1/reports/dashboard

POST   /api/v1/ai/summarize                # planned, not built
POST   /api/v1/ai/chat                     # planned, not built
GET    /api/v1/ai/usage                    # planned, not built
```

The only AI endpoint that exists today is `POST /api/v1/appointments/recommend-slot`.

Every one of these has (or will have) a full request/response schema defined in the corresponding module spec.
