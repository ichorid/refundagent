"""Canonical event append protocol and scoped event-stream loading."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from saferefund import clock, ids
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import (
    CanonicalEvent,
    EventType,
    build_canonical_event,
    payload_as_dict,
)
from saferefund.domain.tables import EventRow, EventSequenceRow
from saferefund.repositories.customers import lock_customer_for_update
from saferefund.repositories.relational_scope import validate_event_relational_scope


@dataclass(frozen=True, slots=True)
class StoredEvent:
    """One persisted canonical event loaded from the append-only log."""

    id: str
    customer_id: str
    case_id: str | None
    order_id: str | None
    seq: int
    event_type: EventType
    actor: Actor
    channel: Channel
    payload: dict[str, Any]
    created_at: datetime


def stored_event_from_row(event_row: EventRow) -> StoredEvent:
    """Convert a persistence row into the repository event view."""
    return StoredEvent(
        id=event_row.id,
        customer_id=event_row.customer_id,
        case_id=event_row.case_id,
        order_id=event_row.order_id,
        seq=event_row.seq,
        event_type=EventType(event_row.type),
        actor=event_row.actor,
        channel=event_row.channel,
        payload=event_row.payload,
        created_at=event_row.created_at,
    )


async def _ensure_event_sequence_row(
    session: AsyncSession,
    customer_id: str,
) -> None:
    """Insert a zeroed counter row when this customer has not appended yet."""
    await session.execute(
        text(
            "INSERT INTO event_sequences (customer_id, next_seq) "
            "VALUES (:customer_id, 0) ON CONFLICT DO NOTHING"
        ),
        {"customer_id": customer_id},
    )


async def _allocate_customer_event_seq(
    session: AsyncSession,
    customer_id: str,
) -> int:
    """Atomically reserve the next per-customer event sequence number."""
    update_statement = (
        update(EventSequenceRow)
        .where(EventSequenceRow.customer_id == customer_id)
        .values(next_seq=EventSequenceRow.next_seq + 1)
        .returning(EventSequenceRow.next_seq)
    )
    allocated_seq = await session.scalar(update_statement)
    if allocated_seq is not None:
        return int(allocated_seq)

    await _ensure_event_sequence_row(session, customer_id)
    allocated_seq = await session.scalar(update_statement)
    if allocated_seq is None:
        message = f"failed to allocate event sequence for customer {customer_id}"
        raise RuntimeError(message)
    return int(allocated_seq)


async def append_canonical_event(  # noqa: PLR0913
    session: AsyncSession,
    *,
    event_type: EventType | str,
    customer_id: str,
    case_id: str | None = None,
    order_id: str | None = None,
    actor: Actor,
    channel: Channel,
    payload: dict[str, Any] | BaseModel,
) -> StoredEvent:
    """Lock the customer, validate the event, assign seq, and persist one row."""
    await lock_customer_for_update(session, customer_id)

    canonical_event: CanonicalEvent = build_canonical_event(
        event_type,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=actor,
        channel=channel,
        payload=payload,
    )
    await validate_event_relational_scope(
        session,
        customer_id=canonical_event.customer_id,
        case_id=canonical_event.case_id,
        order_id=canonical_event.order_id,
    )
    next_seq = await _allocate_customer_event_seq(session, customer_id)
    created_at = clock.now()

    event_row = EventRow(
        id=ids.event_id(),
        customer_id=canonical_event.customer_id,
        case_id=canonical_event.case_id,
        order_id=canonical_event.order_id,
        seq=next_seq,
        type=canonical_event.event_type.value,
        actor=canonical_event.actor,
        channel=canonical_event.channel,
        payload=payload_as_dict(canonical_event),
        created_at=created_at,
    )
    session.add(event_row)
    await session.flush()
    return stored_event_from_row(event_row)


async def load_customer_events(
    session: AsyncSession,
    customer_id: str,
) -> list[StoredEvent]:
    """Load the full customer event stream in ascending sequence order."""
    statement = (
        select(EventRow)
        .where(EventRow.customer_id == customer_id)
        .order_by(EventRow.seq.asc())
    )
    event_rows = await session.scalars(statement)
    return [stored_event_from_row(event_row) for event_row in event_rows]


async def load_case_events(
    session: AsyncSession,
    case_id: str,
) -> list[StoredEvent]:
    """Load only events scoped to one case, in ascending sequence order."""
    statement = (
        select(EventRow).where(EventRow.case_id == case_id).order_by(EventRow.seq.asc())
    )
    event_rows = await session.scalars(statement)
    return [stored_event_from_row(event_row) for event_row in event_rows]
