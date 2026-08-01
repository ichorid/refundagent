"""Async SQLite engine and schema lifecycle operations."""

from sqlalchemy import event
from sqlalchemy.engine.interfaces import DBAPIConnection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import ConnectionPoolEntry

from saferefund.domain.tables import Base

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///./saferefund.db"


def create_database_engine(database_url: str = DEFAULT_DATABASE_URL) -> AsyncEngine:
    """Create an async engine with SQLite foreign-key enforcement enabled."""
    database_engine = create_async_engine(database_url)
    if database_url.startswith("sqlite"):
        event.listen(
            database_engine.sync_engine, "connect", _enable_sqlite_foreign_keys
        )
    return database_engine


def _enable_sqlite_foreign_keys(
    connection: DBAPIConnection,
    _: ConnectionPoolEntry,
) -> None:
    cursor = connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_session_factory(
    database_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Create sessions that do not expire rows after commit."""
    return async_sessionmaker(database_engine, expire_on_commit=False)


async def create_all(database_engine: AsyncEngine) -> None:
    """Create every application table without migration tooling."""
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


async def reset_database(database_engine: AsyncEngine) -> None:
    """Recreate all tables for an isolated test or deterministic demo."""
    async with database_engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)
        await connection.run_sync(Base.metadata.create_all)


async def dispose_database(database_engine: AsyncEngine) -> None:
    """Release database connections after a test or application shutdown."""
    await database_engine.dispose()
