"""FastAPI dependency injection — the application's composition root.

Split by what is being injected:

- :mod:`~app.api.dependencies.db` — request-scoped database session
- :mod:`~app.api.dependencies.repositories` — one provider per repository
- :mod:`~app.api.dependencies.services` — one provider per service
- :mod:`~app.api.dependencies.auth` — ``get_current_user``, ``require_permission``
- :mod:`~app.api.dependencies.ai` — ``AIService`` provider

Usage::

    from app.api.dependencies import get_db_session, get_user_repository
"""

from app.api.dependencies.db import get_db_session
from app.api.dependencies.repositories import (
    get_audit_log_repository,
    get_hospital_repository,
    get_mrn_sequence_repository,
    get_patient_repository,
    get_permission_repository,
    get_refresh_token_repository,
    get_role_repository,
    get_user_repository,
)
from app.api.dependencies.services import (
    get_audit_service,
    get_audit_sink,
    get_hospital_service,
    get_mrn_service,
    get_patient_service,
)

__all__ = [
    "get_audit_log_repository",
    "get_audit_service",
    "get_audit_sink",
    "get_db_session",
    "get_hospital_repository",
    "get_hospital_service",
    "get_mrn_sequence_repository",
    "get_mrn_service",
    "get_patient_repository",
    "get_patient_service",
    "get_permission_repository",
    "get_refresh_token_repository",
    "get_role_repository",
    "get_user_repository",
]
