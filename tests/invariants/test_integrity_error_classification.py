"""IntegrityError recovery must confirm an open-refund race before denying."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.adapters import payment
from saferefund.domain.enums import Actor, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import ActionDeniedPayload
from saferefund.domain.tables import CaseRow, RefundRow
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import Deny
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import find_open_refund_for_order
from saferefund.repositories.seed import ORD_1001_ID, ORD_1003_ID, SOPHIE_CUSTOMER_ID


async def _open_case(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
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


async def _link_order(
    session: AsyncSession,
    *,
    case_id: str,
    order_id: str,
) -> None:
    await execute_agent_action(session, case_id, GetOrders(action="get_orders"))
    await execute_agent_action(
        session,
        case_id,
        LinkOrder(action="link_order", order_id=order_id),
    )


async def test_unrelated_integrity_error_is_not_logged_as_a_policy_rule(
    api_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sequence or FK failures must propagate; they are not R_OPEN_REFUND.

    Mutation: delete the find_open_refund_for_order guard in execute_propose_refund;
    unrelated IntegrityError is appended as R_OPEN_REFUND and this test fails.
    """
    unrelated_error = IntegrityError(
        statement="INSERT INTO events",
        params={},
        orig=Exception("uq_event_customer_seq"),
    )

    async def raise_on_refund_proposed_event(*_args: object, **_kwargs: object) -> None:
        raise unrelated_error

    monkeypatch.setattr(
        "saferefund.gate.refund._append_refund_proposed",
        raise_on_refund_proposed_event,
    )

    async with api_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-f4-unrelated",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1003_ID)
        with pytest.raises(IntegrityError):
            await execute_agent_action(
                session,
                "case_sophie",
                ProposeRefund(action="propose_refund", amount=Decimal("40.00")),
            )

    assert len(payment.calls) == 0

    async with api_session_factory() as session:
        assert await find_open_refund_for_order(session, ORD_1003_ID) is None
        case_events = await load_case_events(session, "case_sophie")
        for event in case_events:
            if event.event_type is EventType.ACTION_DENIED:
                denied_payload = ActionDeniedPayload.model_validate(event.payload)
                assert denied_payload.rule != "R_OPEN_REFUND"


async def test_genuine_open_refund_race_still_becomes_r_open_refund(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A competing live refund on the linked order still maps to R_OPEN_REFUND.

    Mutation: always re-raise IntegrityError in execute_propose_refund; this test fails.
    """
    async with api_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-f4-race",
        )
        await _link_order(session, case_id="case_sophie", order_id=ORD_1001_ID)
        session.add(
            RefundRow(
                id="rfnd_f4_race_existing",
                customer_id=SOPHIE_CUSTOMER_ID,
                order_id=ORD_1001_ID,
                case_id="case_sophie",
                amount=Decimal("5.00"),
                status=RefundStatus.PENDING_APPROVAL,
                approval_expires_at=clock.now()
                + timedelta(seconds=config.APPROVAL_TTL_SECONDS),
                created_at=clock.now(),
            )
        )
        await session.flush()
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("12.00")),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_OPEN_REFUND"
    assert len(payment.calls) == 0

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        denied_event = case_events[-1]
        assert denied_event.event_type is EventType.ACTION_DENIED
        denied_payload = ActionDeniedPayload.model_validate(denied_event.payload)
        assert denied_payload.rule == "R_OPEN_REFUND"
        assert denied_payload.action == "propose_refund"
