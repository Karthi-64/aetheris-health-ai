# 04 — Technology Stack

Every technology choice, with rationale. If it's not on this list, it's not approved. If you want to add something, propose it in a docs PR.

---

## Backend

| Layer | Choice | Version | Why |
|---|---|---|---|
| Language | Python | 3.11+ | Team fluency, mature ecosystem, best AI SDK support |
| Web framework | FastAPI | ^0.109 | Async, typed, OpenAPI out of the box, great DX |
| ORM | SQLAlchemy | 2.x async | Industry standard, mature, flexible |
| Migrations | Alembic | latest | Ships with SQLAlchemy |
| Database | PostgreSQL | 15+ | JSONB, robust, healthcare-grade features |
| Cache / broker | Redis | 7+ | Sessions, cache, rate limits, pub/sub |
| Validation | Pydantic | v2 | Native to FastAPI, fast, typed |
| Auth tokens | PyJWT | latest | Standard JWT |
| Password hashing | Argon2 (via passlib) | latest | Modern, resistant, tunable |
| HTTP client | httpx | latest | Async; the Groq AI provider calls its HTTP API directly with it (no vendor SDK) |
| Background jobs | Celery or RQ | TBD (Phase 1: RQ; revisit for scale) | Simple to start, upgradable |
| Task scheduling | APScheduler + Redis lock | latest | For nightly reports, cleanups |
| Testing | pytest, pytest-asyncio | latest | Standard |
| HTTP testing | httpx test client | latest | Async-friendly |
| Linting | ruff | latest | Fast, replaces flake8 + isort |
| Formatting | ruff format (or black) | latest | Consistent style |
| Type checking | mypy | latest | Strict mode on services |
| Logging | structlog | latest | Structured JSON |
| Metrics | prometheus-client | latest | Standard |
| Containerization | Docker + docker-compose | latest | Deploy target |

## Frontend

| Layer | Choice | Version | Why |
|---|---|---|---|
| Framework | React | 18+ | Ecosystem, team fluency |
| Language | TypeScript | 5+ | Type safety |
| Build tool | Vite | latest | Fast dev, modern |
| Styling | TailwindCSS | 3+ | Utility-first, consistent design tokens |
| Components | Shadcn UI | latest | Composable, accessible, no runtime lock-in |
| State (server) | React Query (TanStack Query) | v5 | Best-in-class server state |
| State (client) | Zustand or React Context | latest | Minimal, opinion-free |
| Forms | React Hook Form + Zod | latest | Fast, ergonomic, validated |
| Routing | React Router | v6 | Standard |
| Icons | Lucide React | latest | Consistent with Shadcn |
| Charts | Recharts | latest | Composable, TS-friendly |
| Date handling | date-fns | latest | Tree-shakeable |
| HTTP client | Axios (via react-query) | latest | Interceptors for auth |
| Testing | Vitest + React Testing Library | latest | Vite-native |

> Note: v1 used Next.js. v2 uses Vite + React because we do not need SSR for the staff-facing hospital app. If we later need public marketing pages or a patient portal with SEO, we spin those up as a separate Next.js app that talks to the same API.

## AI Layer

> **Status (2026-10-06).** One provider is connected (Groq) and one AI capability is built (appointment slot recommendation, confirmed by a member of staff). Rows marked *Planned* are approved choices that are not built yet. Details: [`08-AI_ARCHITECTURE.md`](08-AI_ARCHITECTURE.md) §0.

| Concern | Choice | Status | Why |
|---|---|---|---|
| Provider abstraction | In-house adapter interface (`app/ai/providers/base.py`) | Implemented | Vendor independence |
| Provider connected | Groq, via its OpenAI-compatible chat-completions API | **Implemented — the only connected provider** | Low latency for a small, constrained task |
| Current model | `openai/gpt-oss-20b` on Groq (a reasoning model) for the `fast` hint; override with `AI_FAST_MODEL` | Implemented | Replaced `llama-3.1-70b-versatile`, which Groq decommissioned |
| Provider client | `httpx` directly — **no vendor SDK is installed** (`groq`, `openai`, `anthropic` are not dependencies) | Implemented | One HTTP call with explicit timeouts; full control over what is sent, read and logged |
| Providers (next) | Anthropic (Claude), OpenAI | Planned — adapter stubs only, not connected | Redundancy across vendors |
| Providers (later) | Google Gemini, self-hosted (Ollama/vLLM) | Planned — Ollama is a stub; no Gemini adapter exists | Cost control, data residency |
| Provider fallback / retry | Secondary provider on failure | Planned — today a call is made once and a failure is returned as a typed error | Availability |
| Prompt storage | Version-controlled YAML in `app/ai/prompts/templates/` | Implemented | Reviewable, diffable |
| Structured output | JSON Schema `response_format` sent to the provider, plus server-side validation of every reply | Implemented | The schema narrows the answer; the server check is what is trusted |
| Function calling | Native provider tool APIs, unified in AI Service | Planned — no tools are registered or used | Reuse existing tooling |
| Streaming | SSE | Planned | Perceived performance for long answers |
| MCP | Anthropic MCP SDK (Python) | Planned | Future agent surface |
| Vector store (RAG) | pgvector on the same PostgreSQL | Planned | One DB to operate |
| Embeddings | Provider default (e.g. Voyage, OpenAI, or open-source via sentence-transformers) | Planned | Swappable |
| AI observability | Structured log line per call (structlog): provider, model, tokens, latency, outcome | Implemented (logs only) | Portable |
| AI observability (later) | `ai_interactions` table, dashboards, optional Langfuse/Helicone | Planned | Cost and quality tracking |
| Cost controls | Per-user rate limit on AI endpoints and a process-wide concurrency cap | Implemented | Bounded spend without a budget system |
| Budgets | Per-hospital / per-user AI budgets | Planned | Cost control per tenant |
| Evaluation harness | pytest-based golden set + `deepeval` (optional) | Planned — no golden set exists | CI-integrable |

## Infrastructure

| Concern | Choice | Why |
|---|---|---|
| Container runtime | Docker | Universal |
| Orchestration (MVP) | docker-compose | Simplest |
| Orchestration (Enterprise) | Kubernetes | When needed |
| Reverse proxy | Nginx or Caddy | HTTPS termination |
| Object storage | S3-compatible (MinIO for local, S3/R2 for prod) | Files, PDFs, images |
| Secret management | Env vars → HashiCorp Vault (future) | Progressive hardening |
| CI/CD | GitHub Actions | Where the repo lives |
| Monitoring | Prometheus + Grafana; Sentry for errors | Standard |
| Log aggregation | Loki or hosted (Datadog / Better Stack) | Deferred choice |

## Developer Experience

| Concern | Choice |
|---|---|
| Package manager (Python) | uv (or pip + venv) |
| Package manager (JS) | pnpm |
| Git hooks | pre-commit (ruff, mypy, prettier, eslint) |
| API docs | FastAPI auto-generated OpenAPI + Swagger UI |
| Schema docs | dbdocs.io or generated with SchemaSpy |
| Documentation format | Markdown; served with MkDocs Material in v2.1 |

---

## Decision Log

Every non-obvious technology choice should have an entry here so we don't relitigate it.

### PostgreSQL over MySQL / MongoDB
- Need JSONB for flexible clinical fields
- Native full-text search
- pgvector for RAG
- Strong healthcare adoption
- ACID guarantees non-negotiable for billing and inventory

### FastAPI over Django / Flask
- Native async for AI streaming and long-running jobs
- Typed by default
- OpenAPI generation without extra work

### Modular Monolith over Microservices (day 1)
- We are a small team
- Distributed systems tax is real
- Module boundaries are architected so extraction is mechanical when it's needed

### Vite + React over Next.js
- No SSR needed for the internal app
- Faster dev experience
- Separate marketing/public stack can be Next.js later

### Groq/Anthropic/OpenAI (multi-provider) over single vendor
- Vendor risk is unacceptable for healthcare
- Cost optimization requires switching
- Latency profiles differ per model — different tasks want different models
- **Status (2026-10-06):** this is the target. Today only Groq is connected; the adapter interface is what keeps the others a contained addition. There is no fallback provider yet

### Direct HTTP (httpx) over vendor SDKs for the first provider
- Groq exposes an OpenAI-compatible API, so one small adapter covers it without a new dependency
- Explicit control of timeouts, redirects and proxy behaviour, and of exactly which response fields are read and logged
- Revisit per provider: an SDK is still a reasonable choice where it earns its place (each new dependency is a review decision)

### pgvector over a dedicated vector DB (Pinecone/Weaviate)
- One database to operate
- Sufficient for MVP scale
- Migration path exists if we outgrow it

### RQ (not Celery) for MVP background jobs
- Simpler to operate
- Adequate for MVP throughput
- Migrate to Celery or Temporal when we need workflows, retries, and scheduling primitives at scale

---

## What we're explicitly NOT using (yet, and why)

| Not using | Why | Revisit when |
|---|---|---|
| Kubernetes | Ops overhead too high for MVP | 20+ hospitals or multi-region |
| GraphQL | REST is enough; team fluency higher | Client demands it |
| gRPC internal | Overkill for a monolith | We extract services |
| Kafka | RQ + Redis is enough | Event volume justifies it |
| MongoDB | We don't have document workloads that PostgreSQL can't handle | Never, probably |
| Serverless functions | Cold starts and vendor lock-in | Specific use cases |
| Custom fine-tuned models | Prompting + RAG covers 95% | Domain accuracy demands it |
