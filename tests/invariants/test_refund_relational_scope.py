"""Refund enforcement rows must share one customer across case and order scope."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from sqlalchemy.exc import IntegrityError

from saferefund import config
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import RefundStatus
from saferefund.domain.tables import RefundRow
from saferefund.repositories.seed import (
    ORD_1003_ID,
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    seed_database,
)
from tests.invariants.scenario import open_case_row, propose_refund_awaiting_approval

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SOPHIE_CASE_ID = "case_refund_scope_sophie"


@pytest.fixture
async def refund_scope_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'refund_scope.db'}"
    )
    await create_all(database_engine)
    session_factory = create_session_factory(database_engine)
    async with session_factory.begin() as session:
        await seed_database(session)
    yield session_factory
    await dispose_database(database_engine)


async def _open_sophie_case(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    opening_message_id: str,
) -> None:
    async with session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id=opening_message_id,
        )


async def test_supported_refund_creation_persists_matching_scope(
    refund_scope_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Production refund creation must bind case, order, and customer consistently."""
    refund_id = await propose_refund_awaiting_approval(
        refund_scope_session_factory,
        case_id=SOPHIE_CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-refund-scope-supported",
        amount=Decimal("780.00"),
    )

    async with refund_scope_session_factory() as session:
        refund_row = await session.get(RefundRow, refund_id)
        assert refund_row is not None
        assert refund_row.customer_id == SOPHIE_CUSTOMER_ID
        assert refund_row.case_id == SOPHIE_CASE_ID
        assert refund_row.order_id == ORD_1003_ID


async def test_raw_refund_insert_rejects_cross_customer_case_and_order(
    refund_scope_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Database composite foreign keys must reject mismatched refund scope."""
    await _open_sophie_case(
        refund_scope_session_factory,
        case_id=SOPHIE_CASE_ID,
        opening_message_id="msg-refund-scope-sophie",
    )
    now = datetime(2030, 1, 1, tzinfo=UTC)
    with pytest.raises(IntegrityError):
        async with refund_scope_session_factory.begin() as session:
            session.add(
                RefundRow(
                    id="rfnd_cross_customer",
                    customer_id=SOPHIE_CUSTOMER_ID,
                    order_id=ORD_2001_ID,
                    case_id=SOPHIE_CASE_ID,
                    amount=Decimal("60.00"),
                    status=RefundStatus.PENDING_APPROVAL,
                    approval_expires_at=now
                    + timedelta(seconds=config.APPROVAL_TTL_SECONDS),
                    created_at=now,
                )
            )
