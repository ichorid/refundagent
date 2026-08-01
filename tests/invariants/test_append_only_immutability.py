"""Append-only and seed rows reject in-place updates and deletes at the ORM layer.

Persisted ``EventRow`` evidence and ``CustomerRow`` / ``OrderRow`` seed identity
must not drift: flush attempts to mutate or delete those rows raise
``ImmutableRowError`` before commit.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from saferefund.domain.tables import CustomerRow, EventRow, OrderRow
from saferefund.repositories.events import load_customer_events
from saferefund.repositories.seed import ORD_1001_ID, SOPHIE_CUSTOMER_ID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


def _immutable_row_error() -> type[Exception]:
    from saferefund.domain import tables

    error_type = getattr(tables, "ImmutableRowError", None)
    assert error_type is not None, (
        "domain.tables must define ImmutableRowError and raise it for writes to "
        "append-only and seed tables"
    )
    return cast("type[Exception]", error_type)


async def _first_event_row(session: AsyncSession) -> EventRow:
    stored_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
    event_row = await session.get(EventRow, stored_events[0].id)
    assert event_row is not None
    return event_row


async def test_updating_a_persisted_event_is_rejected(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The event log must reject in-place edits of persisted evidence."""
    immutable_row_error = _immutable_row_error()

    async with seeded_session_factory() as session:
        event_row = await _first_event_row(session)
        event_row.payload = {"method": "tampered"}
        with pytest.raises(immutable_row_error):
            await session.flush()


async def test_deleting_a_persisted_event_is_rejected(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Append-only means deletes are refused as firmly as updates."""
    immutable_row_error = _immutable_row_error()

    async with seeded_session_factory() as session:
        event_row = await _first_event_row(session)
        await session.delete(event_row)
        with pytest.raises(immutable_row_error):
            await session.flush()


async def test_updating_a_customer_seed_row_is_rejected(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Customer seeds are immutable at runtime; identity must not drift."""
    immutable_row_error = _immutable_row_error()

    async with seeded_session_factory() as session:
        customer_row = await session.get(CustomerRow, SOPHIE_CUSTOMER_ID)
        assert customer_row is not None
        customer_row.email = "attacker@example.com"
        with pytest.raises(immutable_row_error):
            await session.flush()


async def test_updating_an_order_seed_row_is_rejected(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Order seeds are immutable at runtime; the refundable total must not drift."""
    immutable_row_error = _immutable_row_error()

    async with seeded_session_factory() as session:
        order_row = await session.get(OrderRow, ORD_1001_ID)
        assert order_row is not None
        order_row.item = "rewritten by an untrusted path"
        with pytest.raises(immutable_row_error):
            await session.flush()
