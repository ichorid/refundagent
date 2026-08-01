"""Pytest fixtures: in-memory SQLite, seed, frozen clock, HTTP client."""

import sys
from collections.abc import Generator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool
from starlette.testclient import TestClient

from saferefund import clock, db, ids
from saferefund.adapters import reset_adapters
from saferefund.agent import HeuristicModel
from saferefund.db import _enable_sqlite_foreign_keys, seed
from saferefund.main import create_app
from saferefund.models import Base

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

FIXED_NOW = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)


@pytest.fixture
def engine() -> Engine:
    """One in-memory SQLite engine shared across a test via StaticPool."""
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(test_engine, "connect", _enable_sqlite_foreign_keys)
    Base.metadata.create_all(test_engine)
    return test_engine


@pytest.fixture
def session(engine: Engine) -> Generator[Session]:
    """Yield a database session bound to the in-memory engine."""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    with session_factory() as db_session:
        yield db_session


@pytest.fixture
def seeded_session(session: Session) -> Session:
    """Session with Sophie/Tom fixture rows."""
    seed(session)
    session.commit()
    return session


@pytest.fixture(autouse=True)
def frozen_clock_and_adapters() -> Generator[None]:
    """Freeze time and reset adapters for every test."""
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(FIXED_NOW)
    reset_adapters()
    yield
    clock.reset_now_for_tests()


@pytest.fixture
def client(engine: Engine) -> Generator[TestClient]:
    """Synchronous HTTP client over the ASGI app with a test database."""
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    original_engine = db.engine
    original_session_local = db.SessionLocal
    db.engine = engine
    db.SessionLocal = session_factory

    app = create_app(model=HeuristicModel())

    with TestClient(app) as http_client:
        yield http_client

    db.engine = original_engine
    db.SessionLocal = original_session_local


@pytest.fixture
def seeded_client(client: TestClient, seeded_session: Session) -> TestClient:
    """HTTP client with seeded fixture data."""
    return client
