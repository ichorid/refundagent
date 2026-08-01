from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, ids
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import Actor, Channel, OrderStatus, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow, CustomerRow, EventRow, OrderRow, RefundRow
from saferefund.repositories.cases import (
    find_case_for_customer_by_opening_message_id,
    find_verification_request_by_token,
)
from saferefund.repositories.customers import find_customer_by_email
from saferefund.repositories.events import (
    StoredEvent,
    append_canonical_event,
    load_case_events,
    load_customer_events,
    stored_event_from_row,
)
from saferefund.repositories.orders import find_order_by_id, list_orders_for_customer
from saferefund.repositories.refunds import (
    find_open_refund_for_order,
    find_refund_by_id,
)


@pytest.fixture
async def repository_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    ids.reset_counter_for_tests()
    clock.reset_now_for_tests()
    clock.set_now_for_tests(datetime(2030, 1, 1, 12, 0, tzinfo=UTC))

    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'repositories.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)

    async with session_factory.begin() as session:
        session.add_all(
            [
                CustomerRow(
                    id="cust_sophie",
                    email="sophie@example.com",
                    name="Sophie Dubois",
                ),
                OrderRow(
                    id="ORD-1001",
                    customer_id="cust_sophie",
                    item="Espresso machine",
                    total=Decimal("249.00"),
                    status=OrderStatus.DELIVERED_DAMAGED,
                ),
                OrderRow(
                    id="ORD-1002",
                    customer_id="cust_sophie",
                    item="Coffee beans 1kg",
                    total=Decimal("24.00"),
                    status=OrderStatus.DELIVERED,
                ),
                CaseRow(
                    id="case_alpha",
                    customer_id="cust_sophie",
                    opening_message_id="message-alpha",
                    created_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
                CaseRow(
                    id="case_beta",
                    customer_id="cust_sophie",
                    opening_message_id="message-beta",
                    created_at=datetime(2030, 1, 2, tzinfo=UTC),
                ),
            ]
        )

    yield session_factory
    clock.reset_now_for_tests()
    await dispose_database(database_engine)


async def _append_case_opened(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    opening_message_id: str,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload={"opening_message_id": opening_message_id},
    )


async def _load_order_events(
    session: AsyncSession,
    order_id: str,
) -> list[StoredEvent]:
    """Test-local mirror of the removed repository order-event loader."""
    statement = (
        select(EventRow)
        .where(EventRow.order_id == order_id)
        .order_by(EventRow.seq.asc())
    )
    event_rows = await session.scalars(statement)
    return [stored_event_from_row(event_row) for event_row in event_rows]


async def test_customer_lookup_normalizes_email(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory() as session:
        customer_row = await find_customer_by_email(session, "Sophie@Example.COM")

    assert customer_row is not None
    assert customer_row.id == "cust_sophie"


async def test_find_case_for_customer_by_opening_message_id(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory() as session:
        case_row = await find_case_for_customer_by_opening_message_id(
            session,
            "cust_sophie",
            "message-beta",
        )

    assert case_row is not None
    assert case_row.id == "case_beta"


async def test_list_orders_for_customer_and_find_order_by_id(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory() as session:
        customer_orders = await list_orders_for_customer(session, "cust_sophie")
        order_row = await find_order_by_id(session, "ORD-1002")

    assert [order.id for order in customer_orders] == ["ORD-1001", "ORD-1002"]
    assert order_row is not None
    assert order_row.item == "Coffee beans 1kg"


async def test_customer_event_stream_is_ordered_by_sequence(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_alpha",
            opening_message_id="message-alpha",
        )
        await append_canonical_event(
            session,
            event_type=EventType.EMAIL_RECEIVED,
            customer_id="cust_sophie",
            case_id="case_alpha",
            actor=Actor.CUSTOMER,
            channel=Channel.EMAIL,
            payload={
                "message_id": "message-alpha",
                "subject": "Damaged machine",
                "body": "The espresso machine arrived damaged.",
            },
        )
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_beta",
            opening_message_id="message-beta",
        )

    async with repository_session_factory() as session:
        customer_events = await load_customer_events(session, "cust_sophie")

    assert [event.event_type for event in customer_events] == [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.CASE_OPENED,
    ]
    assert [event.seq for event in customer_events] == [1, 2, 3]


async def test_case_event_loader_does_not_leak_other_cases(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    verification_expires_at = clock.now() + timedelta(minutes=15)

    async with repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_alpha",
            opening_message_id="message-alpha",
        )
        await append_canonical_event(
            session,
            event_type=EventType.ORDERS_LISTED,
            customer_id="cust_sophie",
            case_id="case_alpha",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={"order_ids": ["ORD-1001", "ORD-1002"]},
        )
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_beta",
            opening_message_id="message-beta",
        )
        await append_canonical_event(
            session,
            event_type=EventType.VERIFICATION_REQUESTED,
            customer_id="cust_sophie",
            case_id="case_beta",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={
                "token": "vtok_beta",
                "expires_at": verification_expires_at,
            },
        )

    async with repository_session_factory() as session:
        alpha_case_events = await load_case_events(session, "case_alpha")
        beta_case_events = await load_case_events(session, "case_beta")

    assert [event.event_type for event in alpha_case_events] == [
        EventType.CASE_OPENED,
        EventType.ORDERS_LISTED,
    ]
    assert [event.event_type for event in beta_case_events] == [
        EventType.CASE_OPENED,
        EventType.VERIFICATION_REQUESTED,
    ]


async def test_order_event_loader_does_not_leak_other_orders(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_alpha",
            opening_message_id="message-alpha",
        )
        await append_canonical_event(
            session,
            event_type=EventType.ORDER_LINKED,
            customer_id="cust_sophie",
            case_id="case_alpha",
            order_id="ORD-1001",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={"order_id": "ORD-1001"},
        )
        await append_canonical_event(
            session,
            event_type=EventType.ORDER_LINKED,
            customer_id="cust_sophie",
            case_id="case_alpha",
            order_id="ORD-1002",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={"order_id": "ORD-1002"},
        )
        await append_canonical_event(
            session,
            event_type=EventType.REFUND_PROPOSED,
            customer_id="cust_sophie",
            case_id="case_alpha",
            order_id="ORD-1001",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={"refund_id": "rfnd_1001", "amount": Decimal("50.00")},
        )

    async with repository_session_factory() as session:
        order_1001_events = await _load_order_events(session, "ORD-1001")
        order_1002_events = await _load_order_events(session, "ORD-1002")

    assert [event.event_type for event in order_1001_events] == [
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
    ]
    assert [event.event_type for event in order_1002_events] == [EventType.ORDER_LINKED]


async def test_find_verification_request_by_token_returns_issuing_case(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    verification_expires_at = clock.now() + timedelta(minutes=15)

    async with repository_session_factory.begin() as session:
        await _append_case_opened(
            session,
            customer_id="cust_sophie",
            case_id="case_beta",
            opening_message_id="message-beta",
        )
        await append_canonical_event(
            session,
            event_type=EventType.VERIFICATION_REQUESTED,
            customer_id="cust_sophie",
            case_id="case_beta",
            actor=Actor.AGENT,
            channel=Channel.INTERNAL,
            payload={
                "token": "vtok_beta",
                "expires_at": verification_expires_at,
            },
        )

    async with repository_session_factory() as session:
        verification_lookup = await find_verification_request_by_token(
            session,
            "vtok_beta",
        )

    assert verification_lookup is not None
    assert verification_lookup.case_id == "case_beta"
    assert verification_lookup.customer_id == "cust_sophie"
    assert verification_lookup.token == "vtok_beta"
    assert verification_lookup.expires_at == verification_expires_at


async def test_refund_repository_finds_rows_by_id_and_open_status(
    repository_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with repository_session_factory.begin() as session:
        session.add(
            RefundRow(
                id="rfnd_open",
                customer_id="cust_sophie",
                order_id="ORD-1001",
                case_id="case_alpha",
                amount=Decimal("100.00"),
                status=RefundStatus.PENDING_APPROVAL,
                approval_expires_at=clock.now() + timedelta(minutes=15),
                created_at=clock.now(),
            )
        )

    async with repository_session_factory() as session:
        refund_row = await find_refund_by_id(session, "rfnd_open")
        open_refund_row = await find_open_refund_for_order(session, "ORD-1001")

    assert refund_row is not None
    assert open_refund_row is not None
    assert open_refund_row.id == "rfnd_open"
