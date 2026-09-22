# ──────────────────────────────────────────────────────────────────────────────
# Aetheris Health AI — Backend Dockerfile
#
# Multi-stage build:
#   1. builder   — Install dependencies and compile.
#   2. runtime   — Minimal production image with only what's needed to run.
# ──────────────────────────────────────────────────────────────────────────────

# ── Stage 1: Builder ─────────────────────────────────────────────────────────
FROM python:3.13-slim AS builder

LABEL stage=builder

# Prevent Python from writing .pyc files and buffering stdout
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Install build dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        gcc \
        libpq-dev \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install uv for fast dependency resolution
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

# Copy dependency files
COPY pyproject.toml ./

# Install all dependencies (including dev, so we can run tests in CI)
RUN uv pip install --system ".[dev]"

# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.13-slim AS runtime

# Security: run as non-root user
RUN groupadd -r aetheris && useradd -r -g aetheris aetheris

# Install only runtime system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libpq5 \
        && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.13/site-packages /usr/local/lib/python3.13/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY . .

# Ensure the app package is importable
RUN python -c "import app; print(f'App package loaded: {app.__name__}')"

# Set ownership to non-root user
RUN chown -R aetheris:aetheris /app

USER aetheris

# Health check — verifies the service responds to HTTP requests
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/v1/health/live')" || exit 1

EXPOSE 8000

# Dependencies were installed with `uv pip install --system`, so the console
# scripts live in /usr/local/bin and are on PATH directly. `uv` itself is only
# present in the builder stage — invoking it here failed with "uv: executable
# file not found", and `uv run` would in any case try to build a fresh project
# venv on top of the packages already installed.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
