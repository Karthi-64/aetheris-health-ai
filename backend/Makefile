# ──────────────────────────────────────────────────────────────────────────────
# Aetheris Health AI — Backend Makefile
#
# Common development commands. Run from the backend/ directory.
# ──────────────────────────────────────────────────────────────────────────────

.PHONY: install dev lint format test test-cov migrate migration seed db-shell shell \
        clean docker-build docker-run help

# ── Variables ────────────────────────────────────────────────────────────────
PYTHON ?= python3
UV ?= uv
PYTEST ?= $(UV) run pytest
RUFF ?= $(UV) run ruff
MYPY ?= $(UV) run mypy
ALEMBIC ?= $(UV) run alembic
UVICORN ?= $(UV) run uvicorn

# ── Help ─────────────────────────────────────────────────────────────────────
help: ## Show this help message
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-20s\033[0m %s\n", $$1, $$2}'

# ── Installation ─────────────────────────────────────────────────────────────
install: ## Install all dependencies (including dev)
	$(UV) sync --all-extras

install-prod: ## Install only production dependencies
	$(UV) sync --no-dev

# ── Development Server ───────────────────────────────────────────────────────
dev: ## Start the development server with hot-reload
	cp -n .env.example .env 2>/dev/null || true
	$(UVICORN) app.main:app --reload --host 0.0.0.0 --port 8000

worker: ## Start the Arq background worker (no-show sweeper)
	$(UV) run arq app.background.worker.WorkerSettings

dev-no-reload: ## Start the development server without hot-reload
	$(UVICORN) app.main:app --host 0.0.0.0 --port 8000

# ── Linting ──────────────────────────────────────────────────────────────────
lint: ## Run all linters (ruff check + mypy)
	$(RUFF) check app/
	$(MYPY) app/

lint-fix: ## Auto-fix lint issues
	$(RUFF) check --fix app/

format: ## Format code with ruff
	$(RUFF) format app/

format-check: ## Check formatting without changing files
	$(RUFF) format --check app/

# ── Testing ──────────────────────────────────────────────────────────────────
test: ## Run all tests with coverage
	$(PYTEST) app/tests/ -v --cov=app --cov-report=term-missing --cov-report=html

test-unit: ## Run unit tests only
	$(PYTEST) app/tests/unit/ -v

test-short: ## Run tests without coverage (faster)
	$(PYTEST) app/tests/ -v

test-watch: ## Run tests in watch mode
	$(PYTEST) app/tests/ -v --looponfail

# ── Database Migrations ──────────────────────────────────────────────────────
migrate: ## Apply pending migrations
	$(ALEMBIC) upgrade head

migration: ## Create a new migration (usage: make migration name="add_users_table")
	@read -p "Migration name: " name; \
	$(ALEMBIC) revision --autogenerate -m "$$name"

migration-manual: ## Create an empty migration for manual editing
	@read -p "Migration name: " name; \
	$(ALEMBIC) revision -m "$$name"

downgrade: ## Rollback the last migration
	$(ALEMBIC) downgrade -1

downgrade-all: ## Rollback all migrations
	$(ALEMBIC) downgrade base

history: ## Show migration history
	$(ALEMBIC) history

current: ## Show current migration version
	$(ALEMBIC) current

# ── Seeding ──────────────────────────────────────────────────────────────────
seed: ## Seed permissions, roles, and realistic demo data (idempotent)
	$(UV) run python -m app.seeds.seed

# ── Database Shell ───────────────────────────────────────────────────────────
db-shell: ## Open a PostgreSQL shell (requires psql)
	@echo "Connecting to database..."
	@psql "$$(grep DATABASE_URL .env | head -1 | cut -d '=' -f2- | sed 's/+asyncpg//')" || \
		echo "Make sure psql is installed and DATABASE_URL is set in .env"

# ── Python Shell ─────────────────────────────────────────────────────────────
shell: ## Open an IPython shell with the app loaded
	$(UV) run ipython -i -c "import sys; sys.path.insert(0, '.'); from app.core.config import settings; from app.database import Base; print('App loaded. settings and Base are available.')"

# ── Cleanup ──────────────────────────────────────────────────────────────────
clean: ## Clean up cache, build artifacts, and virtual environments
	rm -rf .venv/
	rm -rf __pycache__/
	rm -rf .pytest_cache/
	rm -rf .mypy_cache/
	rm -rf *.egg-info/
	rm -rf htmlcov/
	rm -rf .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "Clean complete."

# ── Docker ───────────────────────────────────────────────────────────────────
docker-build: ## Build the Docker image
	docker build -t aetheris-backend:latest .

docker-run: ## Run the Docker image locally
	docker run --rm -p 8000:8000 \
		--env-file .env \
		--name aetheris-backend \
		aetheris-backend:latest

# ── Extras ───────────────────────────────────────────────────────────────────
requirements: ## Export frozen requirements.txt from uv.lock
	$(UV) pip freeze > requirements.txt

update: ## Update all dependencies
	$(UV) sync --all-extras --upgrade

check: lint test ## Run lint + test (CI entry point)
