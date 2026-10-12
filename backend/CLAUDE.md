# Backend Context (Python / FastAPI)

You are in the backend of Aetheris Health AI. The root `../CLAUDE.md` is in effect — read it first if you haven't. This file adds backend-specific rules.

---

## Stack

- **Language:** Python 3.12
- **Framework:** FastAPI
- **ORM:** SQLAlchemy 2.0 (async)
- **Migrations:** Alembic
- **DB:** PostgreSQL 16
- **Cache/queue:** Redis 7
- **Package manager:** `uv`
- **Test runner:** `pytest` + `pytest-asyncio`
- **Formatter/linter:** `ruff`
- **Type checker:** `mypy --strict`

---

## Directory Layout

Canonical map: [`docs/09-PROJECT_STRUCTURE.md`](../docs/09-PROJECT_STRUCTURE.md).

```
backend/
├── app/
│   ├── api/v1/            # FastAPI routers
│   ├── api/dependencies/  # DI composition root (db, repositories, services, auth, ai)
│   ├── core/              # config, security, error codes, envelope, exceptions, logging
│   ├── database/          # declarative base, async engine, session factory
│   ├── models/            # SQLAlchemy models (one module per aggregate)
│   ├── repositories/      # data access (<domain>_repository.py per aggregate)
│   ├── services/          # business logic (<domain>_service.py per aggregate)
│   ├── schemas/           # Pydantic DTOs
│   ├── ai/                # AI platform layer (providers, prompts, tools)
│   ├── middleware/        # request id, logging, rate limit, CORS
│   ├── utils/             # pure helpers
│   ├── background/        # background jobs (worker, scheduler, jobs/)
│   ├── seeds/             # idempotent seed data
│   ├── mcp/               # MCP tool wrappers (v2.1+)
│   ├── tests/             # unit/, repository/, api/, integration/, ai_eval/
│   └── main.py            # FastAPI app factory
├── migrations/versions/   # Alembic migrations (append-only after merge)
├── pyproject.toml         # packaging, pytest, coverage
├── ruff.toml              # linter/formatter config
└── mypy.ini               # type checker config
```

---

## The Layer Rule (memorize this)

```
Route (api/) ──▶ Service (services/) ──▶ Repository (repositories/) ──▶ DB
```

- **Routes** parse input (Pydantic), call a service, wrap the result in the response envelope. Nothing else.
- **Services** enforce business rules, orchestrate multiple repositories, own transactions.
- **Repositories** run queries and return domain objects. One repository per aggregate root.
- **A repository NEVER calls another repository.** The service composes.

If you find yourself importing `PatientRepository` inside `AppointmentRepository`, stop. The logic belongs in a service.

---

## Multi-Tenancy Enforcement

- The base repository automatically injects `hospital_id` into every query using the caller's context.
- Get the current hospital via `get_current_hospital_id()` from `app/core/tenancy.py`.
- If you need to query across tenants (Superadmin only), use `UnscopedRepository` and add an explicit permission check + audit log.
- **Every new table must have `hospital_id UUID NOT NULL REFERENCES hospitals(id)`.**

---

## Migrations (Alembic)

- Generate: `make migration name=add_patient_allergies`
- Edit the generated file before committing — auto-generation is a starting point, not the answer
- Every migration is one atomic change (add table OR add column, not both)
- Never `alter_column` on a column that has data without a data migration
- Once merged, never edit. Add a new migration to fix.
- Migrations must be reversible (`downgrade()` filled in) unless clearly one-way (data destruction)

---

## Async Patterns

- All endpoints are `async def`
- All service methods that touch the DB are `async def`
- All repository methods are `async def`
- Use `AsyncSession`; never mix sync sessions
- Do not block the event loop — no `time.sleep`, `requests`, `psycopg2` sync calls
- Long-running work → background task via Celery/Arq (not inline)

---

## Testing

- **Unit tests:** service layer with mocked repositories. Fast, no DB.
- **Repository tests:** real DB, transactional rollback per test.
- **API tests:** real app, real DB, transactional rollback. Auth via test JWT helper.
- **Every service method has a happy path + error path test.**
- **Every repository method has at least one test that verifies `hospital_id` filtering.**
- **Every mutating endpoint has a test verifying the audit log entry was created.**

Coverage floor: 70% overall, 100% on `app/core/security.py` and `app/services/billing_service.py`.

---

## Common Pitfalls

- **Every `relationship()` must pass `foreign_keys=`.** The audit mixins put
  `created_by` / `updated_by` / `deleted_by` foreign keys to `users.id` on
  nearly every table, so almost any relationship involving `users` has more
  than one FK path and SQLAlchemy raises `AmbiguousForeignKeysError` at mapper
  configuration — which fails at import time, not at query time. Name the join
  column explicitly: `foreign_keys="UserRole.user_id"`. We do this on *all*
  relationships, not just the currently-ambiguous ones, so adding a second FK
  later can't silently change a join.
- **Annotations that FastAPI reads must be runtime imports, not
  `if TYPE_CHECKING:`.** FastAPI resolves endpoint and dependency signatures
  against the module's real globals, so a TYPE_CHECKING-only import raises
  `NameError` when the route registers. `ruff --fix` will try to move them —
  `app/api/**` is exempted from the `TC` rules in `ruff.toml` to prevent that.
- Forgetting `await` on async calls (returns a coroutine, not a value)
- Using `session.query()` (that's 1.x style) — use `select()` + `session.execute()`
- Committing inside a repository — services own transactions
- Returning SQLAlchemy models from services — return domain dicts or Pydantic response models
- Passing `hospital_id` manually into repository methods — the base repository does this
- Import cycles between models — resolve with `TYPE_CHECKING` imports

---

## AI Module Usage

When calling AI from another module's service:

```python
from app.ai.service import AIService

class PatientService:
    def __init__(self, ..., ai_service: AIService):
        self.ai = ai_service
    
    async def summarize_history(self, patient_id):
        # AI usage always:
        # 1. Goes through AIService (not raw provider SDK)
        # 2. Respects hospital budget (planned — no budget is enforced yet)
        # 3. Is logged as one `ai_interaction` line (the ai_interactions table is planned)
        # 4. Uses a versioned prompt from the registry
        return await self.ai.run(
            prompt_key="patient.summarize_history",
            variables={"patient": patient_data},
            hospital_id=hospital_id,
        )
```

**Never** call a model provider directly outside `app/ai/providers/`. Today only Groq is connected, over `httpx` (no vendor SDK is installed); the one real caller is `app/services/slot_ranker.py` — read it before following the sketch above, whose `ai.run(...)` API is illustrative.

---

## Definition of Done for a Backend Feature

- [ ] Migration written, reviewed, applies cleanly
- [ ] Model, repository, service, schema, router all in place
- [ ] Unit tests for service (mocked repository)
- [ ] Integration tests for API endpoints
- [ ] Multi-tenant isolation test
- [ ] Audit log test (for mutating endpoints)
- [ ] OpenAPI docs render correctly
- [ ] Permission dependency wired
- [ ] `make test-backend` green
- [ ] `make lint` green
- [ ] `mypy --strict` clean
