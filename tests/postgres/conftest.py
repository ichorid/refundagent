"""PostgreSQL fixtures for concurrency evidence.

Tests in this package require a disposable PostgreSQL 16.4 instance. The tested
PostgreSQL version is pinned in ``TESTED_POSTGRESQL_VERSION`` and named in every
concurrency claim; results do not generalise beyond that version.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import text

from saferefund import clock, ids
from saferefund.adapters import reset_adapters_for_tests
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.db import (
    create_database_engine,
    create_session_factory,
    dispose_database,
    reset_database,
)
from saferefund.repositories.seed import SeedProfile, seed_database
from tests.conftest import FIXED_TEST_NOW, reset_deterministic_primitives

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

TESTED_POSTGRESQL_VERSION = "16.4"
DEFAULT_POSTGRES_URL = (
    "postgresql+asyncpg://saferefund:saferefund@localhost:54329/saferefund_test"
)
POSTGRES_URL_ENV = "SAFEREFUND_TEST_POSTGRES_URL"


def postgres_database_url() -> str:
    """Resolve the disposable PostgreSQL URL from the environment."""
    return os.environ.get(POSTGRES_URL_ENV, DEFAULT_POSTGRES_URL)


async def _assert_postgresql_version(database_engine: AsyncEngine) -> None:
    async with database_engine.connect() as connection:
        version_row = await connection.execute(text("SHOW server_version"))
        server_version = version_row.scalar_one()
    major_minor = ".".join(server_version.split(".")[:2])
    expected_major_minor = ".".join(TESTED_POSTGRESQL_VERSION.split(".")[:2])
    assert major_minor == expected_major_minor, (
        f"PostgreSQL concurrency evidence targets {TESTED_POSTGRESQL_VERSION}; "
        f"connected server reports {server_version}"
    )


@pytest.fixture(scope="session")
def postgres_database_url_fixture() -> str:
    """Expose the configured PostgreSQL URL for stress targets and diagnostics."""
    return postgres_database_url()


@pytest.fixture
async def postgres_engine(
    postgres_database_url_fixture: str,
) -> AsyncIterator[AsyncEngine]:
    """Provide one async PostgreSQL engine per test function."""
    database_engine = create_database_engine(postgres_database_url_fixture)
    try:
        await _assert_postgresql_version(database_engine)
        yield database_engine
    finally:
        await dispose_database(database_engine)


@pytest.fixture
def postgres_session_factory(
    postgres_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory bound to the disposable PostgreSQL engine."""
    return create_session_factory(postgres_engine)


@pytest.fixture
async def postgres_seeded_session_factory(
    postgres_session_factory: async_sessionmaker[AsyncSession],
    postgres_engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Reset schema synchronously, then load the standard seed profile."""
    reset_deterministic_primitives()
    await reset_database(postgres_engine)
    async with postgres_session_factory.begin() as session:
        await seed_database(session, profile=SeedProfile.STANDARD)
    return postgres_session_factory


@pytest.fixture(autouse=True)
def _postgres_reset_process_state() -> None:
    """Keep adapter and clock state deterministic across PostgreSQL proofs."""
    reset_adapters_for_tests()
    reset_case_locks_for_tests()
    reset_deterministic_primitives()
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(FIXED_TEST_NOW)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every module under tests/postgres as requiring PostgreSQL."""
    for item in items:
        if "/tests/postgres/" in item.nodeid.replace("\\", "/"):
            item.add_marker(pytest.mark.postgres)
