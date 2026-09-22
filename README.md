# Aetheris Health AI

Enterprise AI-first Hospital Management Platform. Multi-tenant SaaS for
appointments, patient records, billing, clinical decision support, and
operations — built with FastAPI, React 18, PostgreSQL, and Redis.

---

## Repository Structure

```
aetheris-health-ai/
├── backend/            # FastAPI API, services, repositories, migrations
│   ├── app/            # Application source (API, services, models, schemas)
│   ├── migrations/     # Alembic database migrations
│   ├── tests/          # Unit, integration, and API tests
│   └── Dockerfile      # Production multi-stage build
├── frontend/           # React 18 + Vite + Tailwind + Shadcn UI
│   ├── src/            # Application source
│   ├── Dockerfile      # Development image (Vite dev server)
│   └── package.json
├── docker-compose.yml  # Local development stack (Postgres, Redis, backend, frontend)
├── Makefile            # Developer commands
├── docs/               # Project documentation
│   ├── modules/        # Module specs
│   └── *.md            # Architecture, tech stack, API standards, etc.
└── .python-version     # Pins Python 3.13
```

---

## Prerequisites

| Tool | Version | Purpose |
|------|---------|---------|
| Docker | 24+ | Container runtime for all 4 services |
| Docker Compose | v2 | Multi-container orchestration |
| Python | 3.13 | Backend runtime (pinned in `.python-version`) |
| uv | latest | Python package/dependency management |
| Node.js | 20+ | Frontend runtime |
| npm | 10+ | Frontend package manager |
| Make | any | Developer command runner |

---

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url> && cd aetheris-health-ai

# 2. Start all services (Postgres, Redis, backend, frontend)
make up

# 3. Wait for healthchecks (~15s), then apply migrations
make migrate

# 4. Seed demo data (hospital, admin user, sample data)
make seed

# 5. Install frontend dependencies (needed for local tests/lint)
cd frontend && npm ci
```

**That's it.** Open the URLs below to see the application.

---

## Service URLs

| Service | URL | Notes |
|---------|-----|-------|
| Frontend | http://localhost:5173 | Vite dev server with HMR |
| Backend API | http://localhost:8000 | FastAPI + Uvicorn |
| API Docs | http://localhost:8000/docs | Swagger UI (auto-generated) |
| PostgreSQL | localhost:5432 | Database: `aetheris`, User: `aetheris` |
| Redis | localhost:6380 | Rate limiting, caching |

---

## Environment Configuration

The Docker Compose stack provides sensible development defaults. No `.env`
file is required to start.

For the full list of configurable environment variables, see
[`backend/.env.example`](backend/.env.example).

Key variables used by Docker Compose:

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | `postgresql+asyncpg://aetheris:aetheris@postgres:5432/aetheris` | Database connection |
| `REDIS_URL` | `redis://redis:6379/0` | Redis connection (internal Docker port 6379, mapped to host 6380) |
| `APP_ENV` | `development` | Runtime environment |
| `APP_DEBUG` | `true` | Enable debug mode |

---

## Make Commands

Run `make help` for the full list:

| Command | Description |
|---------|-------------|
| `make up` | Start Postgres, Redis, backend, and frontend |
| `make down` | Stop all services |
| `make logs` | Tail service logs |
| `make migrate` | Apply database migrations |
| `make seed` | Seed demo data |
| `make test` | Run all backend + frontend tests |
| `make lint` | Run all lint and type checks |
| `make format` | Format backend code |
| `make dev` | Start frontend dev server |
| `make backend-test` | Backend tests only |
| `make backend-lint` | Backend lint + type checks only |
| `make frontend-test` | Frontend tests only |
| `make frontend-lint` | Frontend lint only |
| `make frontend-build` | Build frontend for production |

---

## Frontend Development

The frontend runs inside Docker as part of `make up`. The Vite dev server
is accessible at `http://localhost:5173` with hot module replacement (HMR).

The Vite proxy automatically forwards `/api` requests to the backend
container (`http://backend:8000` inside Docker). When running the frontend
natively outside Docker, it falls back to `http://localhost:8000`.

**To rebuild the frontend image** (e.g. after `package.json` changes):

```bash
docker compose up -d --build frontend
```

---

## Database

**Migrations:**

```bash
make migrate    # applies all pending Alembic migrations
```

**Seeding:**

```bash
make seed       # creates demo hospital, admin user, sample data
```

The seed is idempotent — safe to run multiple times.

---

## Testing

```bash
make test           # run all tests (backend + frontend)
make backend-test   # backend only (pytest)
make frontend-test  # frontend only (vitest)
```

---

## Linting & Formatting

```bash
make lint       # backend (ruff + mypy) + frontend (eslint)
make format     # backend code formatting (ruff)
```

---

## Shutdown

```bash
make down       # stop and remove containers (data persists in volumes)
```

To completely reset the database:

```bash
make down
docker volume rm aetheris-health-ai_pgdata
make up
make migrate
make seed
```

---

## Documentation

All project documentation lives in the [`docs/`](docs/) directory:

| Document | Purpose |
|----------|---------|
| [PRD](docs/01-PRD.md) | Product requirements |
| [Architecture](docs/03-ARCHITECTURE.md) | System design and boundaries |
| [Tech Stack](docs/04-TECH_STACK.md) | Technology choices |
| [Database Design](docs/05-DATABASE_DESIGN.md) | Schema and conventions |
| [API Standards](docs/06-API_STANDARDS.md) | Endpoint conventions |
| [Security](docs/07-SECURITY.md) | Security requirements |
| [Project Structure](docs/09-PROJECT_STRUCTURE.md) | File layout |
| [Development Guide](docs/10-DEVELOPMENT_GUIDE.md) | How to add modules |
| [Testing Strategy](docs/11-TESTING_STRATEGY.md) | Test coverage requirements |
| [Sprint Plan](docs/15-SPRINT_PLAN.md) | Current sprint scope |

Module specs are in [`docs/modules/`](docs/modules/).

---

## Troubleshooting

**Port already in use:**
Stop existing services on that port, or change the port mapping in
`docker-compose.yml`.

**Database connection refused:**
Ensure Docker is running and containers are healthy:
```bash
docker compose ps
docker compose exec postgres pg_isready -U aetheris -d aetheris
```

**Backend won't start:**
Check logs: `make logs`. Common issues: pending migrations (`make migrate`),
missing environment variables.

**Frontend can't reach backend:**
Ensure all services are running (`make up`) and healthy (`docker compose ps`).
The Vite proxy forwards `/api` requests to the backend container. If
rebuilding the frontend, use `docker compose up -d --build frontend`.

**Redis connection errors:**
Redis is optional infrastructure. Rate limiting degrades gracefully without
it. Check: `docker compose exec redis redis-cli ping` (Redis is on host port 6380, Docker port 6379).

---

## License

Proprietary — Aetheris Health AI. All rights reserved.
