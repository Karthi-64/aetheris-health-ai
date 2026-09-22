"""Application configuration via Pydantic Settings v2.

All configuration values are loaded from environment variables.
Secrets are never hardcoded — every sensitive value comes from
.env or the process environment.

Usage::

    from app.core.config import settings

    db_url = settings.DATABASE_URL
    debug = settings.APP_DEBUG
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(StrEnum):
    """Valid deployment environments."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    """Supported structured-logging output formats."""

    JSON = "json"
    CONSOLE = "console"


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Values are populated from (in order of precedence):
    1. Environment variables (highest)
    2. .env file
    3. Default values defined below
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        validate_default=True,
    )

    # ── App ────────────────────────────────────────────────────────────────
    APP_NAME: str = Field(
        default="Aetheris Health AI", description="Human-readable application name"
    )
    APP_ENV: AppEnv = Field(default=AppEnv.DEVELOPMENT, description="Deployment environment")
    APP_DEBUG: bool = Field(
        default=True, description="Enable debug mode (stack traces in responses)"
    )
    APP_BASE_URL: str = Field(
        default="http://localhost:8000", description="Public base URL of the API"
    )
    APP_SECRET_KEY: str = Field(
        default="change-me-to-a-long-random-string-in-production",
        description="Secret key for signing internal tokens and encryption",
        min_length=32,
    )

    # ── Database ───────────────────────────────────────────────────────────
    DATABASE_URL: str = Field(
        default="postgresql+asyncpg://aetheris:aetheris@localhost:5432/aetheris",
        description="Async PostgreSQL connection string (asyncpg driver)",
    )
    DATABASE_POOL_SIZE: int = Field(
        default=20, ge=1, le=100, description="Maximum connections in the pool"
    )
    DATABASE_MAX_OVERFLOW: int = Field(
        default=10, ge=0, description="Max overflow connections beyond pool_size"
    )
    DATABASE_ECHO: bool = Field(
        default=False, description="Log all SQL statements (development only)"
    )

    # ── Redis ──────────────────────────────────────────────────────────────
    REDIS_URL: str = Field(default="redis://localhost:6379/0", description="Redis connection URL")

    # ── Logging ────────────────────────────────────────────────────────────
    LOG_LEVEL: str = Field(
        default="INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)"
    )
    LOG_FORMAT: LogFormat = Field(
        default=LogFormat.JSON, description="Log output format: json or console"
    )

    # ── CORS ───────────────────────────────────────────────────────────────
    CORS_ORIGINS: list[str] = Field(
        default=["http://localhost:5173", "http://localhost:3000"],
        description="Allowed CORS origins (JSON array string from env)",
    )

    @field_validator("CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> list[str]:
        """Parse CORS_ORIGINS from a JSON string or pass through a list."""
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            import json

            try:
                result = json.loads(value)
                if isinstance(result, list):
                    return result
            except json.JSONDecodeError:
                pass
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return []

    # ── JWT / Auth ─────────────────────────────────────────────────────────
    JWT_PRIVATE_KEY: str | None = Field(
        default=None,
        description="RSA private key for JWT signing (PEM). If None, uses HS256 with APP_SECRET_KEY.",
    )
    JWT_PUBLIC_KEY: str | None = Field(
        default=None,
        description="RSA public key for JWT verification (PEM). If None, uses HS256 with APP_SECRET_KEY.",
    )
    JWT_ISSUER: str = Field(default="aetheris", description="JWT issuer claim (iss)")
    JWT_ACCESS_TTL_SECONDS: int = Field(
        default=900, ge=60, le=86400, description="Access token TTL in seconds (default 15 min)"
    )
    JWT_REFRESH_TTL_SECONDS: int = Field(
        default=604800,
        ge=3600,
        le=2592000,
        description="Refresh token TTL in seconds (default 7 days)",
    )
    JWT_LEEWAY_SECONDS: int = Field(
        default=60, description="Clock skew leeway for JWT validation (seconds)"
    )

    # ── Auth Policy ─────────────────────────────────────────────────────────
    PASSWORD_MIN_LENGTH: int = Field(
        default=12, ge=8, le=128, description="Minimum password length"
    )
    MAX_FAILED_LOGIN_ATTEMPTS: int = Field(
        default=5, ge=1, le=20, description="Max failed attempts before lockout"
    )
    ACCOUNT_LOCKOUT_MINUTES: int = Field(
        default=30, ge=1, le=1440, description="Account lockout duration in minutes"
    )
    PASSWORD_RESET_TOKEN_TTL_MINUTES: int = Field(
        default=30, ge=5, le=1440, description="Password reset token lifetime in minutes"
    )
    INVITE_TOKEN_TTL_HOURS: int = Field(
        default=72, ge=1, le=720, description="Invitation token lifetime in hours (B6 invite seam)"
    )

    # ── Rate Limiting ──────────────────────────────────────────────────────
    RATE_LIMIT_ANON_PER_MIN: int = Field(
        default=60, ge=1, description="Anonymous requests per minute"
    )
    RATE_LIMIT_USER_PER_MIN: int = Field(
        default=300, ge=1, description="Authenticated requests per minute"
    )
    RATE_LIMIT_AI_PER_MIN: int = Field(
        default=30, ge=1, description="AI endpoint requests per minute"
    )
    RATE_LIMIT_HOSPITAL_PER_MIN: int = Field(
        default=1000, ge=1, description="Requests per minute per hospital (tenant)"
    )
    RATE_LIMIT_TRUST_PROXY_HEADER: bool = Field(
        default=False,
        description="Trust X-Forwarded-For for the client IP. Enable ONLY behind a proxy that overwrites the header, otherwise clients can spoof it and evade the anonymous limit.",
    )

    @property
    def is_development(self) -> bool:
        """True when running in development mode."""
        return self.APP_ENV == AppEnv.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        """True when running in production mode."""
        return self.APP_ENV == AppEnv.PRODUCTION


# Singleton — import this, don't instantiate Settings yourself.
settings = Settings()
