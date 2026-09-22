"""Application-wide constants.

Values here never change at runtime. Environment-dependent values
belong in :mod:`app.core.config`.
"""

from __future__ import annotations

from enum import StrEnum

# ── Environment Identifiers ────────────────────────────────────────────────
APP_ENV_DEVELOPMENT = "development"
APP_ENV_STAGING = "staging"
APP_ENV_PRODUCTION = "production"

# ── API Metadata ───────────────────────────────────────────────────────────
API_V1_PREFIX = "/api/v1"
API_DOCS_URL = "/docs"
API_REDOC_URL = "/redoc"
API_OPENAPI_URL = "/openapi.json"

# ── Health Endpoint Paths ──────────────────────────────────────────────────
HEALTH_LIVE_PATH = "/health/live"
HEALTH_READY_PATH = "/health/ready"
HEALTH_PATH = "/health"

# ── Request Context ────────────────────────────────────────────────────────
REQUEST_ID_HEADER = "X-Request-ID"
CORRELATION_ID_HEADER = "X-Correlation-ID"
RESPONSE_TIME_HEADER = "X-Response-Time"

# ── Pagination Defaults ────────────────────────────────────────────────────
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100

# ── Database ───────────────────────────────────────────────────────────────
DATABASE_MODEL_NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

# ── Audit ──────────────────────────────────────────────────────────────────
AUDIT_ACTOR_TYPE_USER = "user"
AUDIT_ACTOR_TYPE_SYSTEM = "system"
AUDIT_ACTOR_TYPE_AI = "ai"

# ── Timeouts ───────────────────────────────────────────────────────────────
DEFAULT_HTTP_TIMEOUT_SECONDS = 30


class AppointmentStatus(StrEnum):
    """Valid appointment lifecycle states."""

    BOOKED = "booked"
    CHECKED_IN = "checked_in"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    NO_SHOW = "no_show"


class UserStatus(StrEnum):
    """Valid user account states."""

    ACTIVE = "active"
    SUSPENDED = "suspended"
    INVITED = "invited"


class InvoiceStatus(StrEnum):
    """Valid invoice lifecycle states."""

    DRAFT = "draft"
    ISSUED = "issued"
    PARTIALLY_PAID = "partially_paid"
    PAID = "paid"
    VOID = "void"
    REFUNDED = "refunded"
