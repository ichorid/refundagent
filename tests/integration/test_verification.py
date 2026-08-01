"""Integration scenarios 6-7: verification blocking and multi-case resumption."""

from decimal import Decimal
from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config
from saferefund.adapters import mailer, payment
from saferefund.agent.gateway import ModelGateway
from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    RefundStatus,
    VerificationMethod,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    CustomerVerifiedPayload,
    RefundExecutedPayload,
    RefundProposedPayload,
    VerificationRequestedPayload,
)
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import ORD_2001_ID, TOM_CUSTOMER_ID, TOM_EMAIL
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway


def _action_json(action: str, **fields: str) -> str:
    parts = [f'"action": "{action}"']
    parts.extend(f'"{key}": "{value}"' for key, value in fields.items())
    return "{" + ", ".join(parts) + "}"


def _scripted_model(*actions: str) -> ModelGateway:
    return scripted_gateway(actions)


def _verification_block_script(*, amount: str = "60.00") -> list[str]:
    return [
        _action_json("propose_refund", amount=amount),
        _action_json("request_verification"),
    ]


def _tom_resume_script() -> list[str]:
    return [
        _action_json("get_orders"),
        _action_json("link_order", order_id=ORD_2001_ID),
        _action_json("propose_refund", amount="60.00"),
        _action_json(
            "send_reply",
            subject="Refund processed",
            body="Your refund has been processed.",
        ),
        _action_json("finish", summary="Tom refund completed after verification."),
    ]


async def _post_tom_email(
    client: AsyncClient,
    *,
    message_id: str,
    subject: str = "Refund request",
    body: str = "Please refund my kettle.",
) -> dict[str, Any]:
    response = await client.post(
        "/inbound-email",
        json={
            "envelope_from": TOM_EMAIL,
            "message_id": message_id,
            "subject": subject,
            "body": body,
        },
    )
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


def _verification_mail_for_token(token: str) -> mailer.OutboxMessage:
    verification_messages = [
        message
        for message in mailer.outbox
        if message.subject == config.VERIFICATION_SUBJECT
    ]
    assert len(verification_messages) == 1
    verification_message = verification_messages[0]
    assert verification_message.to == TOM_EMAIL
    assert verification_message.body == config.VERIFICATION_BODY.format(token=token)
    return verification_message


def _token_verified_events(customer_events: list[Any]) -> list[Any]:
    return [
        event
        for event in customer_events
        if event.event_type is EventType.CUSTOMER_VERIFIED
        and CustomerVerifiedPayload.model_validate(event.payload).method
        is VerificationMethod.TOKEN
    ]


async def test_unverified_blocked_then_verified(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 6: Tom is blocked until token verification, then the loop completes."""
    script = _verification_block_script() + _tom_resume_script()
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_tom_email(
            client,
            message_id="msg-verify-blocked",
            body="I need a refund for my kettle.",
        )

    assert inbound["status"] == CaseStatus.AWAITING_VERIFICATION.value
    case_id = inbound["case_id"]

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)

    denied = ActionDeniedPayload.model_validate(case_events[2].payload)
    assert denied.action == "propose_refund"
    assert denied.rule == "R_VERIFIED"

    verification_event = next(
        event
        for event in case_events
        if event.event_type is EventType.VERIFICATION_REQUESTED
    )
    verification_payload = VerificationRequestedPayload.model_validate(
        verification_event.payload
    )
    _verification_mail_for_token(verification_payload.token)
    assert _token_verified_events(customer_events) == []

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        confirm_response = await client.post(
            "/verification/confirm",
            json={"token": verification_payload.token},
        )

    assert confirm_response.status_code == 200
    confirm_body = confirm_response.json()
    assert confirm_body["customer_id"] == TOM_CUSTOMER_ID
    assert confirm_body["resumed_case_ids"] == [case_id]

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=TOM_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )

    verified_event = _token_verified_events(customer_events)[0]
    assert verified_event.customer_id == TOM_CUSTOMER_ID
    assert verified_event.case_id is None
    assert verified_event.actor is Actor.SYSTEM
    assert verified_event.channel is Channel.VERIFICATION_API

    event_types = [event.event_type for event in case_events]
    assert event_types == [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.ACTION_DENIED,
        EventType.VERIFICATION_REQUESTED,
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_AUTO_APPROVED,
        EventType.REFUND_EXECUTED,
        EventType.REPLY_SENT,
        EventType.CASE_CLOSED,
    ]

    proposed = RefundProposedPayload.model_validate(case_events[6].payload)
    executed = RefundExecutedPayload.model_validate(case_events[8].payload)
    assert executed.refund_id == proposed.refund_id
    assert executed.amount == Decimal("60.00")

    case_closed = CaseClosedPayload.model_validate(case_events[-1].payload)
    assert case_closed.outcome is CaseOutcome.FINISHED

    async with api_session_factory() as session:
        refund_row = await find_refund_by_id(session, proposed.refund_id)
    assert refund_row is not None
    assert refund_row.status is RefundStatus.EXECUTED
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=proposed.refund_id,
            amount=Decimal("60.00"),
        ),
    ]
    assert case_summary.status is CaseStatus.CLOSED


async def test_verification_unblocks_all_cases(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 7: one token confirmation resumes every awaiting case for Tom."""
    block_script = _verification_block_script()
    resume_script = [
        _action_json("finish", summary="Resumed after verification."),
        _action_json("finish", summary="Second case resumed."),
    ]

    first_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*block_script),
    )
    second_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*block_script),
    )
    confirm_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*resume_script),
    )

    async with AsyncClient(
        transport=ASGITransport(app=first_app),
        base_url="http://test",
    ) as client:
        first = await _post_tom_email(
            client,
            message_id="msg-verify-first",
            subject="Case one",
            body="First refund request.",
        )

    async with AsyncClient(
        transport=ASGITransport(app=second_app),
        base_url="http://test",
    ) as client:
        second = await _post_tom_email(
            client,
            message_id="msg-verify-second",
            subject="Case two",
            body="Second refund request.",
        )

    first_case_id = first["case_id"]
    second_case_id = second["case_id"]
    assert first["status"] == CaseStatus.AWAITING_VERIFICATION.value
    assert second["status"] == CaseStatus.AWAITING_VERIFICATION.value

    async with api_session_factory() as session:
        first_events = await load_case_events(session, first_case_id)
        verification_event = next(
            event
            for event in first_events
            if event.event_type is EventType.VERIFICATION_REQUESTED
        )
        token = VerificationRequestedPayload.model_validate(
            verification_event.payload
        ).token

    async with AsyncClient(
        transport=ASGITransport(app=confirm_app),
        base_url="http://test",
    ) as client:
        confirm_response = await client.post(
            "/verification/confirm",
            json={"token": token},
        )

    assert confirm_response.status_code == 200
    confirm_body = confirm_response.json()
    assert confirm_body["customer_id"] == TOM_CUSTOMER_ID
    assert confirm_body["resumed_case_ids"] == [first_case_id, second_case_id]

    async with api_session_factory() as session:
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
        first_summary = project_case_summary(
            case_id=first_case_id,
            customer_id=TOM_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )
        second_summary = project_case_summary(
            case_id=second_case_id,
            customer_id=TOM_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )

    assert first_summary.status is not CaseStatus.AWAITING_VERIFICATION
    assert second_summary.status is not CaseStatus.AWAITING_VERIFICATION
    assert first_summary.status is CaseStatus.CLOSED
    assert second_summary.status is CaseStatus.CLOSED

    verified_event = _token_verified_events(customer_events)[0]
    assert verified_event.case_id is None
    assert verified_event.customer_id == TOM_CUSTOMER_ID
    verified_payload = CustomerVerifiedPayload.model_validate(verified_event.payload)
    assert verified_payload.method is VerificationMethod.TOKEN
