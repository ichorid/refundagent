"""Canonical event append must reject cross-customer case and order scope."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.tables import EventRow
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.relational_scope import InvalidEventRelationalScopeError
from saferefund.repositories.seed import (
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    TOM_CUSTOMER_ID,
    seed_database,
)
from tests.invariants.scenario import open_case_row

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SOPHIE_CASE_ID = "case_sophie_scope"
TOM_CASE_ID = "case_tom_scope"


@pytest.fixture
async def relational_scope_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'relational_scope.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session)
        await open_case_row(
            session,
            case_id=SOPHIE_CASE_ID,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-scope",
        )
        await open_case_row(
            session,
            case_id=TOM_CASE_ID,
            customer_id=TOM_CUSTOMER_ID,
            opening_message_id="msg-tom-scope",
        )
    yield session_factory
    await dispose_database(database_engine)


async def _event_count(session: AsyncSession) -> int:
    return int(await session.scalar(select(func.count()).select_from(EventRow)) or 0)


@pytest.mark.parametrize(
    ("customer_id", "case_id", "order_id", "event_type", "payload", "actor", "channel"),
    [
        pytest.param(
            SOPHIE_CUSTOMER_ID,
            TOM_CASE_ID,
            None,
            EventType.EMAIL_RECEIVED,
            {
                "message_id": "msg-foreign-case",
                "subject": "Refund",
                "body": "Please help.",
            },
            Actor.CUSTOMER,
            Channel.EMAIL,
            id="sophie_customer_with_tom_case",
        ),
        pytest.param(
            SOPHIE_CUSTOMER_ID,
            TOM_CASE_ID,
            ORD_2001_ID,
            EventType.ORDER_LINKED,
            {"order_id": ORD_2001_ID},
            Actor.AGENT,
            Channel.INTERNAL,
            id="sophie_customer_with_tom_order",
        ),
        pytest.param(
            SOPHIE_CUSTOMER_ID,
            SOPHIE_CASE_ID,
            ORD_2001_ID,
            EventType.ORDER_LINKED,
            {"order_id": ORD_2001_ID},
            Actor.AGENT,
            Channel.INTERNAL,
            id="sophie_case_with_tom_order",
        ),
        pytest.param(
            SOPHIE_CUSTOMER_ID,
            SOPHIE_CASE_ID,
            ORD_2001_ID,
            EventType.REFUND_PROPOSED,
            {"refund_id": "rfnd_scope_conflict", "amount": "60.00"},
            Actor.AGENT,
            Channel.INTERNAL,
            id="refund_lifecycle_cross_customer_case_order",
        ),
    ],
)
async def test_event_append_rejects_foreign_case_or_order(  # noqa: PLR0913, PLR0917
    relational_scope_session_factory: async_sessionmaker[AsyncSession],
    customer_id: str,
    case_id: str | None,
    order_id: str | None,
    event_type: EventType,
    payload: dict[str, str],
    actor: Actor,
    channel: Channel,
) -> None:
    """Supported append paths must not persist cross-customer relational scope."""
    async with relational_scope_session_factory() as session:
        events_before = await _event_count(session)

    with pytest.raises(InvalidEventRelationalScopeError):
        async with relational_scope_session_factory.begin() as session:
            await append_canonical_event(
                session,
                event_type=event_type,
                customer_id=customer_id,
                case_id=case_id,
                order_id=order_id,
                actor=actor,
                channel=channel,
                payload=payload,
            )

    async with relational_scope_session_factory() as session:
        assert await _event_count(session) == events_before
