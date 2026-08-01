"""Seed data row values, Sophie verification event, and injection fixture parity."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.db import (
    create_database_engine,
    create_session_factory,
    dispose_database,
    reset_database,
)
from saferefund.domain.enums import Actor, Channel, OrderStatus, VerificationMethod
from saferefund.domain.events import EventType
from saferefund.domain.tables import CustomerRow, OrderRow
from saferefund.repositories.customers import find_customer_by_email, normalized_email
from saferefund.repositories.events import load_customer_events
from saferefund.repositories.orders import find_order_by_id, list_orders_for_customer
from saferefund.repositories.seed import (
    INJECTED_ORD_1001_ITEM,
    ORD_1001_ID,
    ORD_1001_ITEM,
    ORD_1002_ID,
    ORD_1003_ID,
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    SOPHIE_NAME,
    TOM_CUSTOMER_ID,
    TOM_EMAIL,
    TOM_NAME,
    UNKNOWN_SENDER_EMAIL,
    SeedProfile,
    seed_database,
    seed_order_specs_for_profile,
)
from tests.conftest import reset_deterministic_primitives
from tests.unit.seed_helpers import count_seed_rows


@asynccontextmanager
async def seeded_session_factory_for_profile(
    database_path: Path,
    *,
    profile: SeedProfile,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    reset_deterministic_primitives()

    database_engine = create_database_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = create_session_factory(database_engine)
    await reset_database(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session, profile=profile)
    try:
        yield session_factory
    finally:
        await dispose_database(database_engine)


@pytest.mark.parametrize("profile", list(SeedProfile))
async def test_seed_row_counts_are_deterministic_after_database_reset(
    tmp_path: Path,
    profile: SeedProfile,
) -> None:
    database_path = tmp_path / f"seed-{profile.value}.db"
    async with seeded_session_factory_for_profile(
        database_path,
        profile=profile,
    ) as session_factory:
        async with session_factory() as session:
            customer_count, order_count, event_count = await count_seed_rows(session)

        assert customer_count == 2
        assert order_count == 4
        assert event_count == 1


async def test_standard_seed_rows_match_architecture_values(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with seeded_session_factory() as session:
        sophie_row = await session.get(CustomerRow, SOPHIE_CUSTOMER_ID)
        tom_row = await session.get(CustomerRow, TOM_CUSTOMER_ID)
        ord_1001 = await find_order_by_id(session, ORD_1001_ID)
        ord_1002 = await find_order_by_id(session, ORD_1002_ID)
        ord_1003 = await find_order_by_id(session, ORD_1003_ID)
        ord_2001 = await find_order_by_id(session, ORD_2001_ID)
        sophie_orders = await list_orders_for_customer(session, SOPHIE_CUSTOMER_ID)
        mars_lookup = await find_customer_by_email(session, UNKNOWN_SENDER_EMAIL)

    assert sophie_row is not None
    assert sophie_row.email == normalized_email(SOPHIE_EMAIL)
    assert sophie_row.name == SOPHIE_NAME

    assert tom_row is not None
    assert tom_row.email == normalized_email(TOM_EMAIL)
    assert tom_row.name == TOM_NAME

    assert mars_lookup is None

    assert ord_1001 is not None
    assert ord_1001.item == ORD_1001_ITEM
    assert ord_1001.total == Decimal("249.00")
    assert ord_1001.status is OrderStatus.DELIVERED_DAMAGED

    assert ord_1002 is not None
    assert ord_1002.item == "Coffee beans 1kg"
    assert ord_1002.total == Decimal("24.00")
    assert ord_1002.status is OrderStatus.DELIVERED

    assert ord_1003 is not None
    assert ord_1003.item == "Coffee grinder"
    assert ord_1003.total == Decimal("780.00")
    assert ord_1003.status is OrderStatus.DELIVERED

    assert ord_2001 is not None
    assert ord_2001.item == "Electric kettle"
    assert ord_2001.total == Decimal("60.00")
    assert ord_2001.status is OrderStatus.DELIVERED

    assert [order.id for order in sophie_orders] == [
        ORD_1001_ID,
        ORD_1002_ID,
        ORD_1003_ID,
    ]


def test_customers_table_has_no_verified_column() -> None:
    customer_column_names = {column.name for column in CustomerRow.__table__.columns}
    assert "verified" not in customer_column_names


async def test_sophie_verification_comes_from_seed_event_not_a_column(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with seeded_session_factory() as session:
        sophie_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        tom_events = await load_customer_events(session, TOM_CUSTOMER_ID)

    sophie_verification_events = [
        event
        for event in sophie_events
        if event.event_type is EventType.CUSTOMER_VERIFIED
    ]
    tom_verification_events = [
        event for event in tom_events if event.event_type is EventType.CUSTOMER_VERIFIED
    ]

    assert len(sophie_verification_events) == 1
    assert len(tom_verification_events) == 0

    verification_event = sophie_verification_events[0]
    assert verification_event.case_id is None
    assert verification_event.order_id is None
    assert verification_event.actor is Actor.SYSTEM
    assert verification_event.channel is Channel.INTERNAL
    assert verification_event.payload == {"method": VerificationMethod.SEED.value}
    assert verification_event.seq == 1


async def test_injected_seed_differs_only_in_ord_1001_item(tmp_path: Path) -> None:
    async with (
        seeded_session_factory_for_profile(
            tmp_path / "standard.db",
            profile=SeedProfile.STANDARD,
        ) as standard_session_factory,
        standard_session_factory() as standard_session,
    ):
        standard_orders = {
            order.id: order
            for order in (await standard_session.scalars(select(OrderRow))).all()
        }
        _, _, standard_event_count = await count_seed_rows(standard_session)

    async with (
        seeded_session_factory_for_profile(
            tmp_path / "injected.db",
            profile=SeedProfile.INJECTED_ORD_1001_ITEM,
        ) as injected_session_factory,
        injected_session_factory() as injected_session,
    ):
        injected_orders = {
            order.id: order
            for order in (await injected_session.scalars(select(OrderRow))).all()
        }
        _, _, injected_event_count = await count_seed_rows(injected_session)

    assert (
        set(standard_orders)
        == set(injected_orders)
        == {
            ORD_1001_ID,
            ORD_1002_ID,
            ORD_1003_ID,
            ORD_2001_ID,
        }
    )
    assert standard_event_count == injected_event_count == 1

    for order_id in (ORD_1002_ID, ORD_1003_ID, ORD_2001_ID):
        standard_order = standard_orders[order_id]
        injected_order = injected_orders[order_id]
        assert standard_order.customer_id == injected_order.customer_id
        assert standard_order.item == injected_order.item
        assert standard_order.total == injected_order.total
        assert standard_order.status == injected_order.status

    assert standard_orders[ORD_1001_ID].item == ORD_1001_ITEM
    assert injected_orders[ORD_1001_ID].item == INJECTED_ORD_1001_ITEM
    assert (
        standard_orders[ORD_1001_ID].customer_id
        == injected_orders[ORD_1001_ID].customer_id
    )
    assert standard_orders[ORD_1001_ID].total == injected_orders[ORD_1001_ID].total
    assert standard_orders[ORD_1001_ID].status == injected_orders[ORD_1001_ID].status


async def test_seed_order_specs_track_profile_item_text() -> None:
    standard_specs = {
        order_spec.order_id: order_spec
        for order_spec in seed_order_specs_for_profile(SeedProfile.STANDARD)
    }
    injected_specs = {
        order_spec.order_id: order_spec
        for order_spec in seed_order_specs_for_profile(
            SeedProfile.INJECTED_ORD_1001_ITEM
        )
    }

    assert standard_specs[ORD_1001_ID].item == ORD_1001_ITEM
    assert injected_specs[ORD_1001_ID].item == INJECTED_ORD_1001_ITEM
    assert standard_specs[ORD_1002_ID] == injected_specs[ORD_1002_ID]
