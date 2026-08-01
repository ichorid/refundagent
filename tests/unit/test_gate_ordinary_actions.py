"""Unit tests for execute_agent_action on non-refund agent proposals."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import (
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.adapters import mailer, payment, reset_adapters_for_tests, ticketing
from saferefund.domain.enums import Actor, CaseOutcome, CaseStatus, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    OrdersListedPayload,
    ReplySentPayload,
    VerificationRequestedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import execute_agent_action
from saferefund.policy.verdicts import Allow, Deny
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.seed import (
    ORD_1001_ID,
    ORD_1002_ID,
    ORD_1003_ID,
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    TOM_CUSTOMER_ID,
    TOM_EMAIL,
)


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


@pytest.fixture
def gate_actions_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def test_get_orders_appends_orders_listed_with_agent_actor(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-1",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            GetOrders(action="get_orders"),
        )

    assert verdict == Allow()

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        listed_event = case_events[-1]
        assert listed_event.event_type is EventType.ORDERS_LISTED
        assert listed_event.actor is Actor.AGENT
        assert listed_event.channel is Channel.INTERNAL
        listed_payload = OrdersListedPayload.model_validate(listed_event.payload)
        assert listed_payload.order_ids == [ORD_1001_ID, ORD_1002_ID, ORD_1003_ID]


async def test_denied_get_orders_calls_no_adapter(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_tom",
            customer_id=TOM_CUSTOMER_ID,
            opening_message_id="msg-tom-1",
        )
        verdict = await execute_agent_action(
            session,
            "case_tom",
            GetOrders(action="get_orders"),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_VERIFIED"
    assert len(mailer.outbox) == 0
    assert len(payment.calls) == 0
    assert len(ticketing.escalations) == 0


async def test_link_order_appends_order_linked_for_owned_order(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-link",
        )
        await execute_agent_action(
            session,
            "case_sophie",
            GetOrders(action="get_orders"),
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            LinkOrder(action="link_order", order_id=ORD_1001_ID),
        )

    assert verdict == Allow()

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        linked_event = case_events[-1]
        assert linked_event.event_type is EventType.ORDER_LINKED
        assert linked_event.actor is Actor.AGENT
        assert linked_event.order_id == ORD_1001_ID


async def test_send_reply_resolves_recipient_from_case_customer(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-reply",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            SendReply(
                action="send_reply",
                subject="Refund update",
                body="Your refund is complete.",
            ),
        )

    assert verdict == Allow()
    assert len(mailer.outbox) == 1
    assert mailer.outbox[0].to == SOPHIE_EMAIL
    assert mailer.outbox[0].to != "attacker@example.com"

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        reply_event = case_events[-1]
        assert reply_event.event_type is EventType.REPLY_SENT
        assert reply_event.actor is Actor.AGENT
        reply_payload = ReplySentPayload.model_validate(reply_event.payload)
        assert reply_payload.subject == "Refund update"


async def test_request_verification_sends_token_matching_event(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_tom",
            customer_id=TOM_CUSTOMER_ID,
            opening_message_id="msg-tom-verify",
        )
        verdict = await execute_agent_action(
            session,
            "case_tom",
            RequestVerification(action="request_verification"),
        )

    assert verdict == Allow()
    assert len(mailer.outbox) == 1
    assert mailer.outbox[0].to == TOM_EMAIL
    assert mailer.outbox[0].subject == config.VERIFICATION_SUBJECT

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_tom")
        verification_event = case_events[-1]
        assert verification_event.event_type is EventType.VERIFICATION_REQUESTED
        verification_payload = VerificationRequestedPayload.model_validate(
            verification_event.payload
        )
        assert verification_payload.token in mailer.outbox[0].body
        assert verification_payload.expires_at == clock.now() + timedelta(
            seconds=config.VERIFICATION_TTL_SECONDS
        )


async def test_request_verification_denied_when_customer_already_verified(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-verify-deny",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            RequestVerification(action="request_verification"),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_ALREADY_VERIFIED"
    assert len(mailer.outbox) == 0


async def test_finish_appends_case_closed_with_summary(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-finish",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            Finish(action="finish", summary="Resolved after refund."),
        )

    assert verdict == Allow()

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        closed_event = case_events[-1]
        assert closed_event.event_type is EventType.CASE_CLOSED
        assert closed_event.actor is Actor.SYSTEM
        closed_payload = CaseClosedPayload.model_validate(closed_event.payload)
        assert closed_payload.outcome is CaseOutcome.FINISHED
        assert closed_payload.summary == "Resolved after refund."

        case_summary = project_case_summary(
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            events=case_events,
            now=clock.now(),
        )
        assert case_summary.status is CaseStatus.CLOSED


async def test_closed_case_returns_r_case_not_actionable(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-closed",
        )
        await execute_agent_action(
            session,
            "case_sophie",
            Finish(action="finish", summary="Done."),
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            GetOrders(action="get_orders"),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_CASE_NOT_ACTIONABLE"

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        denied_event = case_events[-1]
        denied_payload = ActionDeniedPayload.model_validate(denied_event.payload)
        assert denied_payload.rule == "R_CASE_NOT_ACTIONABLE"


async def test_awaiting_verification_case_returns_r_case_not_actionable(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_tom",
            customer_id=TOM_CUSTOMER_ID,
            opening_message_id="msg-tom-suspended",
        )
        await execute_agent_action(
            session,
            "case_tom",
            RequestVerification(action="request_verification"),
        )
        verdict = await execute_agent_action(
            session,
            "case_tom",
            SendReply(
                action="send_reply",
                subject="While waiting",
                body="Still verifying.",
            ),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_CASE_NOT_ACTIONABLE"
    assert len(mailer.outbox) == 1


async def test_agent_escalate_closes_case_with_ticket(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-escalate",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            Escalate(action="escalate", reason="Need a human"),
        )

    assert verdict == Allow()
    assert len(ticketing.escalations) == 1

    async with gate_actions_session_factory() as session:
        case_events = await load_case_events(session, "case_sophie")
        event_types = [event.event_type for event in case_events]
        assert event_types[-2:] == [EventType.ESCALATED, EventType.CASE_CLOSED]


async def test_denied_refund_proposal_calls_no_money_or_mail_adapter(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-refund-deny",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_NO_LINKED_ORDER"
    assert len(payment.calls) == 0
    assert len(mailer.outbox) == 0


async def test_link_foreign_order_is_denied_without_adapter_effects(
    gate_actions_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with gate_actions_session_factory.begin() as session:
        await _open_case(
            session,
            case_id="case_sophie",
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id="msg-sophie-foreign-order",
        )
        verdict = await execute_agent_action(
            session,
            "case_sophie",
            LinkOrder(action="link_order", order_id=ORD_2001_ID),
        )

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_ORDER_OWNERSHIP"
    assert len(mailer.outbox) == 0
