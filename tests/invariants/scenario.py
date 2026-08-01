"""Shared scenario builders for the architecture-invariant proof tests.

These helpers deliberately mirror the production call paths rather than
reaching around them, so a passing proof test is evidence about the system
and not about the fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from httpx import ASGITransport, AsyncClient

from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.domain.enums import Actor, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import execute_agent_action
from saferefund.main import create_app
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.seed import ORD_1003_ID

if TYPE_CHECKING:
    from httpx import Response
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.agent.gateway import ModelGateway

CASE_CREATED_AT = datetime(2030, 1, 1, tzinfo=UTC)
LARGE_REFUND_AMOUNT = Decimal("780.00")


async def open_case_row(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> None:
    """Create one correlation row plus its case_opened event, leaving it open."""
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


async def propose_refund_awaiting_approval(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
    amount: Decimal = LARGE_REFUND_AMOUNT,
) -> str:
    """Drive a case to one pending_approval refund and return its refund id."""
    async with session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
        )
        await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
        await execute_agent_action(
            session,
            case_id,
            LinkOrder(action="link_order", order_id=ORD_1003_ID),
        )
        await execute_agent_action(
            session,
            case_id,
            ProposeRefund(action="propose_refund", amount=amount),
        )

    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        return approval_required.refund_id


async def post_inbound_email(  # noqa: PLR0913
    session_factory: async_sessionmaker[AsyncSession],
    model_gateway: ModelGateway,
    *,
    envelope_from: str,
    message_id: str,
    subject: str = "Refund request",
    body: str = "My order arrived damaged.",
) -> Response:
    """Post one inbound email through a fresh application instance."""
    app = create_app(session_factory=session_factory, model_gateway=model_gateway)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(
            "/inbound-email",
            json={
                "envelope_from": envelope_from,
                "message_id": message_id,
                "subject": subject,
                "body": body,
            },
        )


def event_types(case_events: list[Any]) -> list[EventType]:
    """Return the ordered event types of a loaded case stream."""
    return [event.event_type for event in case_events]
