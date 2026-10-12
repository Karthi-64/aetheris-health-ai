"""Database infrastructure — declarative base, async engine, session factory, UnitOfWork.

This package holds database *plumbing* only. ORM models live in
:mod:`app.models`; data access lives in :mod:`app.repositories`.

Usage::

    from app.database import Base, UnitOfWork, create_session_factory

    # During startup:
    from app.database import initialize_database
    initialize_database(database_url="postgresql+asyncpg://...")

    # Create a session:
    factory = create_session_factory()
    async with factory() as session:
        result = await session.execute(...)

    # Wrap writes in a transaction:
    uow = UnitOfWork(session)
    async with uow.transaction():
        await repo.create(...)
"""

from app.core.tenancy import install_tenant_guards
from app.database.base_class import Base
from app.database.session import (
    create_session_factory,
    dispose_engine,
    get_async_engine,
    initialize_database,
)
from app.database.unit_of_work import UnitOfWork

__all__ = [
    "Base",
    "UnitOfWork",
    "create_session_factory",
    "dispose_engine",
    "get_async_engine",
    "initialize_database",
]

# Tenant isolation is enforced on every ORM session in the process, not only
# the ones this package creates: the listeners attach to the Session class.
# Importing any model imports this package, so they are always in place.
install_tenant_guards()
