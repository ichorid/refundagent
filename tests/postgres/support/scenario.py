"""Scenario builders for PostgreSQL concurrency proofs."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from saferefund.actions.models import GetOrders, LinkOrder
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import execute_agent_action
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.seed import ORD_1003_ID, SOPHIE_CUSTOMER_ID

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

CASE_CREATED_AT = datetime(2030, 1, 1, tzinfo=UTC)
THRESHOLD_PROBE_AMOUNT = Decimal("300.00")


async def open_case_row(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> None:
    """Create one correlation row plus its case_opened event."""
    session.add(
        CaseRow(
            id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=CASE_CREATED_AT,
        )
    )
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload={"opening_message_id": opening_message_id},
    )


async def link_order_for_case(
    session: AsyncSession,
    *,
    case_id: str,
    order_id: str = ORD_1003_ID,
) -> None:
    """Drive one case through get_orders and link_order."""
    await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
    await execute_agent_action(
        session,
        case_id,
        LinkOrder(action="link_order", order_id=order_id),
    )


async def prepare_threshold_probe_cases(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    first_case_id: str,
    second_case_id: str,
    customer_id: str = SOPHIE_CUSTOMER_ID,
) -> None:
    """Open two cases on the same customer/order for threshold contention."""
    async with session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=first_case_id,
            customer_id=customer_id,
            opening_message_id=f"msg-{first_case_id}",
        )
        await link_order_for_case(session, case_id=first_case_id)
        await open_case_row(
            session,
            case_id=second_case_id,
            customer_id=customer_id,
            opening_message_id=f"msg-{second_case_id}",
        )
        await link_order_for_case(session, case_id=second_case_id)
