"""Run the canonical Sophie refund demo via HTTP and print tables."""

# ruff: noqa: T201

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from httpx import ASGITransport, AsyncClient

from saferefund import clock, ids
from saferefund.adapters import reset_adapters_for_tests
from saferefund.adapters.mailer import snapshot_outbox
from saferefund.agent.gateway import ModelGateway
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.db import (
    DEFAULT_DATABASE_URL,
    create_database_engine,
    create_session_factory,
    dispose_database,
    reset_database,
)
from saferefund.demo_tables import format_event_table, format_outbox_table
from saferefund.main import create_app
from saferefund.repositories.seed import SOPHIE_EMAIL, seed_database

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

DEMO_FIXED_NOW = datetime(2030, 1, 15, 9, 30, tzinfo=UTC)
DEMO_MESSAGE_ID = "msg-demo-sophie-refund"
DEMO_SUBJECT = "Refund please"
DEMO_BODY = "My espresso machine arrived damaged."
DATABASE_PATH = Path("saferefund.db")


def reset_demo_primitives() -> None:
    """Freeze clock and ID generation so repeated demos match structurally."""
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(DEMO_FIXED_NOW)
    reset_adapters_for_tests()
    reset_case_locks_for_tests()


def remove_demo_database_file() -> None:
    """Delete the on-disk SQLite file before schema recreation."""
    if DATABASE_PATH.exists():
        DATABASE_PATH.unlink()


async def prepare_demo_database() -> tuple[
    AsyncEngine, async_sessionmaker[AsyncSession]
]:
    """Drop and recreate tables, then load the standard seed profile."""
    remove_demo_database_file()
    database_engine = create_database_engine(DEFAULT_DATABASE_URL)
    await reset_database(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session)
    return database_engine, session_factory


async def post_canonical_inbound_email(
    session_factory: async_sessionmaker[AsyncSession],
) -> dict[str, Any]:
    """POST the canonical Sophie refund request through the FastAPI route."""
    app = create_app(
        session_factory=session_factory,
        model_gateway=ModelGateway.heuristic_subprocess(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://demo") as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": SOPHIE_EMAIL,
                "message_id": DEMO_MESSAGE_ID,
                "subject": DEMO_SUBJECT,
                "body": DEMO_BODY,
            },
        )
    response.raise_for_status()
    return cast("dict[str, Any]", response.json())


async def run_demo() -> int:
    """Execute the full demo flow and print event and outbox tables."""
    reset_demo_primitives()
    database_engine, session_factory = await prepare_demo_database()
    try:
        inbound_response = await post_canonical_inbound_email(session_factory)
        events = cast("list[dict[str, object]]", inbound_response["events"])

        print(
            format_event_table(
                case_id=str(inbound_response["case_id"]),
                status=str(inbound_response["status"]),
                events=events,
            )
        )
        print()
        print(format_outbox_table(snapshot_outbox()))
        return 0
    finally:
        await dispose_database(database_engine)


def main() -> None:
    """Entry point for `python -m saferefund.demo` and `make demo`."""
    exit_code = asyncio.run(run_demo())
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
