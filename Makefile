# ──────────────────────────────────────────────────────────────────────────────
# Aetheris Health AI — Root Developer Commands
#
# Thin wrapper that delegates to docker-compose (infra + backend) and
# npm (frontend). Run `make help` for the full list.
# ──────────────────────────────────────────────────────────────────────────────

.PHONY: help up down logs migrate seed test lint format dev \
        frontend-dev frontend-test frontend-lint frontend-build backend-test \
        backend-lint

.DEFAULT_GOAL := help

# ── Infrastructure ───────────────────────────────────────────────────────────

up: ## Start PostgreSQL, Redis, and the backend via Docker Compose
	docker compose up -d

down: ## Stop all Docker Compose services
	docker compose down

logs: ## Tail logs from all Docker Compose services
	docker compose logs -f

# ── Database ─────────────────────────────────────────────────────────────────

migrate: ## Apply all pending Alembic migrations
	cd backend && uv run alembic upgrade head

seed: ## Seed the database with demo data (hospital, admin, sample users)
	cd backend && uv run python -m app.seeds.seed

# ── Backend ──────────────────────────────────────────────────────────────────

backend-test: ## Run the backend test suite
	cd backend && uv run pytest app/tests/ -v

backend-lint: ## Run ruff check, ruff format check, and mypy on the backend
	cd backend && uv run ruff check app/ && uv run ruff format --check app/ && uv run mypy app/

# ── Frontend ─────────────────────────────────────────────────────────────────

dev: frontend-dev ## Alias for frontend-dev

frontend-dev: ## Start the Vite development server (http://localhost:5173)
	cd frontend && npm run dev

frontend-test: ## Run the frontend test suite
	cd frontend && npm test

frontend-lint: ## Run ESLint on the frontend
	cd frontend && npm run lint

frontend-build: ## Build the frontend for production
	cd frontend && npm run build

# ── Combined ─────────────────────────────────────────────────────────────────

test: backend-test frontend-test ## Run all backend and frontend tests

lint: backend-lint frontend-lint ## Run all backend and frontend lint checks

format: ## Format backend code with ruff
	cd backend && uv run ruff format app/

# ── Help ─────────────────────────────────────────────────────────────────────

help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'
