# 10 — Development Guide

Everything an engineer (or an AI coding assistant) needs to work on Aetheris Health AI.

---

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | Backend |
| Node.js | 20+ | Frontend |
| pnpm | 9+ | Frontend package manager |
| Docker | 24+ | For PostgreSQL, Redis, MinIO |
| Docker Compose | v2+ | Bundled with modern Docker |
| Git | 2.40+ | With GPG signing configured |
| Make | any | For task shortcuts |

Recommended:
- VS Code with Python, Pylance, ESLint, Prettier, Tailwind CSS IntelliSense extensions
- `uv` for fast Python dependency management

---

## 2. First-Time Setup

```
git clone git@github.com:<org>/aetheris.git
cd aetheris
cp .env.example .env
# fill in the values that don't have defaults (AI provider keys, JWT secret, etc.)

docker compose -f docker-compose.dev.yml up -d   # postgres, redis, minio
make backend-install
make frontend-install
make db-migrate
make db-seed
make dev                                          # runs backend + frontend in parallel
```

Backend runs at `http://localhost:8000` (docs at `/docs`).
Frontend runs at `http://localhost:5173`.

---

## 3. Environment Variables

`.env.example` documents every variable. Categories:

- **App:** `APP_ENV`, `APP_DEBUG`, `APP_BASE_URL`, `APP_SECRET_KEY`
- **Database:** `DATABASE_URL`, `DATABASE_POOL_SIZE`
- **Redis:** `REDIS_URL`
- **JWT:** `JWT_PRIVATE_KEY`, `JWT_PUBLIC_KEY`, `JWT_ISSUER`, `JWT_ACCESS_TTL_SECONDS`, `JWT_REFRESH_TTL_SECONDS`
- **Object storage:** `S3_ENDPOINT`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`, `S3_BUCKET`
- **AI (optional):** `GROQ_API_KEY` turns it on; optional `AI_ENABLED`, `AI_FAST_MODEL`, `GROQ_BASE_URL`, `AI_REQUEST_TIMEOUT_SECONDS`, `GROQ_STRICT_JSON_SCHEMA`, `AI_MAX_CONCURRENT_CALLS` (see `backend/.env.example`). `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` and `GOOGLE_API_KEY` are not read by any code yet
- **Email:** `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`
- **Logging:** `LOG_LEVEL`, `LOG_FORMAT`
- **Rate limits:** `RATE_LIMIT_ANON_PER_MIN`, `RATE_LIMIT_USER_PER_MIN`, `RATE_LIMIT_AI_PER_MIN`

Never commit filled `.env` files. Never share keys in chat.

---

## 4. Backend Workflow

### 4.1 Install dependencies

```
cd backend
uv venv
uv pip install -e ".[dev]"
```

### 4.2 Common commands (Makefile targets at the repo root)

```
make backend-run          # uvicorn app.main:app --reload
make backend-test         # pytest
make backend-test-cov     # pytest with coverage
make backend-lint         # ruff check + mypy
make backend-format       # ruff format
make db-migrate           # alembic upgrade head
make db-migrate-create    # alembic revision --autogenerate -m "..."
make db-downgrade         # alembic downgrade -1
make db-seed              # python -m app.seeds
```

### 4.3 Adding a Module

1. Write / update the module specification in `docs/modules/<name>.md`
2. Get spec review from one other founder
3. Create migration: `make db-migrate-create name="add_<table>"`
4. Add SQLAlchemy model in `app/models/<domain>.py` following conventions
5. Add repository in `app/repositories/<domain>_repository.py`
6. Add Pydantic schemas in `app/schemas/<domain>.py`
7. Add service in `app/services/<domain>_service.py`
8. Wire DI in `app/api/dependencies/repositories.py` and `dependencies/services.py`
9. Add router in `app/api/v1/<domain>.py`
10. Register router in `app/main.py`
11. Add permissions to the seed script and reseed
12. Write tests (unit → repository → API)

Any deviation from these steps requires a docs PR to update the workflow.

### 4.4 Code Style

- **Type hints everywhere.** `mypy --strict` on `app/services/` and `app/ai/`
- **PEP 8** via `ruff format`
- **Docstrings** for every public method — describe intent, not implementation
- **No `print()` in application code.** Use `structlog`.
- **No naked exceptions.** Catch specific types.
- **No global state** other than the app factory registry.
- **Async by default** for anything doing I/O.

### 4.5 Async Patterns

- Every DB call is async
- Every provider call is async
- Never call `asyncio.run()` inside a request handler
- Long-running work → background job, not inline
- Fan-out with `asyncio.gather()` when independent; sequence otherwise

---

## 5. Frontend Workflow

### 5.1 Install

```
cd frontend
pnpm install
```

### 5.2 Common commands

```
make frontend-run         # vite dev
make frontend-build       # vite build
make frontend-test        # vitest
make frontend-lint        # eslint + prettier check
make frontend-format      # prettier write
```

### 5.3 Component Guidelines

- **Shadcn primitives** from `components/ui/`; do not fork casually
- **Compose over customize** — extend via props/composition, not by editing primitives
- **One component per file** unless internally scoped
- **Feature-first folders** — `components/patient/PatientCard.tsx`, not `components/cards/PatientCard.tsx`
- **Data flow:** React Query owns server state; Zustand only for UI-local state; no Redux
- **Never store secrets in the frontend.** Tokens in memory + refresh cookie (v2.1)

### 5.4 API Client

- Generated types from `/openapi.json` via `openapi-typescript`
- Manual client wrappers in `src/api/<module>.ts` for ergonomic hooks
- Axios instance in `src/api/client.ts` handles auth header injection and 401 → refresh flow

### 5.5 Forms & Validation

- React Hook Form + Zod schemas
- Zod schemas may be shared between frontend and backend by generating from OpenAPI, but a duplicated Zod schema is acceptable when it simplifies the code

### 5.6 Styling

- Tailwind utility classes
- Design tokens from `tailwind.config.ts` — no inline hex colors
- Dark mode via `class="dark"` toggle on `<html>`

---

## 6. Git Workflow

- Branch naming: `feat/<module>-<short-desc>`, `fix/<module>-<short-desc>`, `docs/<topic>`, `refactor/<scope>`
- Commit style: Conventional Commits (`feat(patient): add duplicate detection at registration`)
- One PR per module change; keep them under ~500 lines when possible
- Every PR must:
  - Link to the module spec or issue
  - Include or update tests
  - Pass CI (lint, type, tests)
  - Get one non-author approval
- Squash-merge to `main`
- No force-push to `main`

### 6.1 Signed commits

- GPG or SSH signing required
- Enforced by branch protection

---

## 7. Testing

See [`11-TESTING_STRATEGY.md`](11-TESTING_STRATEGY.md) for detail. Quick summary:

- Unit tests for services with mocked repositories
- Repository tests against a real (Docker) PostgreSQL
- API tests via the FastAPI test client
- Integration tests for critical flows (auth, appointment booking, billing)
- AI eval tests on prompt changes

Coverage target: ≥ 70% overall, ≥ 85% on services.

---

## 8. Database Migrations

- Always use Alembic
- One migration per PR when possible
- Reversible migrations preferred
- Data migrations run as separate scripts, not autogenerated
- Never edit an applied migration; add a new one
- Migration review is part of PR review

Local reset:

```
make db-reset             # drops, recreates, migrates, seeds
```

Never run `db-reset` against staging or production.

---

## 9. Working with AI

- Prompts live in `app/ai/prompts/templates/` — edit YAML, don't hardcode
- Bump the `version` field on any prompt change
- Run `make ai-eval PROMPT=<id>` before merging prompt changes
- Every new AI use case needs an evaluation golden set
- Every model call writes one structured `ai_interaction` log line (provider, model, tokens, outcome — never the key, prompt or answer). The `ai_interactions` table is planned and does not exist yet
- Only the Groq provider is connected. The Ollama adapter is an unregistered stub, so there is no local-model option yet

---

## 10. Debugging Tips

- **Structured logs:** every request has an `X-Request-ID`; filter logs by it
- **Slow queries:** enable `DATABASE_ECHO=true` locally
- **Redis inspection:** `make redis-cli`
- **DB inspection:** `make db-shell`
- **AI interactions:** filter the logs for `ai_interaction` with the request_id (there is no `ai_interactions` table yet)
- **Feature flags:** stored as top-level keys of `hospitals.settings`, e.g. `"feature.ai.slot_recommendation": true`; read them with `GET /api/v1/hospitals/current/feature-flags`

---

## 11. Conventions Summary

| Concern | Rule |
|---|---|
| Files | One responsibility each. Short. |
| Imports | Absolute imports from `app.` |
| Naming | Boring. `PatientService`, not `SmartPatientHandler` |
| Comments | Explain **why**, not what |
| TODOs | With a JIRA/issue link; expire in 30 days |
| Prints | Never in application code |
| Silent excepts | Never |
| Magic numbers | Named constants |
| Hardcoded strings | In `constants.py` or as enums |
| Config in code | Never — always env vars |
| Secrets in git | Never |

---

## 12. Getting Help

- Documentation gap → docs PR
- Ambiguous requirement → ask in Slack, then update docs
- Architecture question → refer to `03-ARCHITECTURE.md`, escalate to founders if unresolved
- Blocked on someone else's module → pair up; don't route around
- Broke main → tell the team, revert first, fix in a branch

---

## 13. Working with AI Coding Assistants ("Vibe Coding")

We build with AI. Practical rules:

- **Feed the assistant the relevant module spec + these architecture docs** at the start of a session
- **Never let the assistant introduce new patterns.** If it wants to "simplify" the layered architecture, say no.
- **Every AI-generated file** goes through the same PR review, tests, and mypy checks as human-written code
- **Never commit secrets that appeared in an AI transcript** — assume the transcript is not secret
- **Prompt the assistant to write the test first**, then the implementation
- **When the assistant proposes a schema change**, require it to update the module spec and generate a migration
- **Treat AI-generated commit messages** with the same discipline as human ones
- **You are responsible** for every line the assistant writes on your branch

The point of vibe coding is speed, not laxness. The bar for quality does not drop because we typed less.
