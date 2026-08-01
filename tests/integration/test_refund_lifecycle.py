"""Integration scenarios 1-5: refund lifecycle via HTTP and ScriptedModel."""

from decimal import Decimal
from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.adapters import payment
from saferefund.agent.gateway import ModelGateway
from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    RefundStatus,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CaseClosedPayload,
    EmailReceivedPayload,
    OrderLinkedPayload,
    OrdersListedPayload,
    RefundApprovalRequiredPayload,
    RefundApprovedPayload,
    RefundAutoApprovedPayload,
    RefundExecutedPayload,
    RefundProposedPayload,
    RefundRejectedPayload,
    ReplySentPayload,
)
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import (
    ORD_1001_ID,
    ORD_1003_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
)
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import scripted_gateway


def _action_json(action: str, **fields: str) -> str:
    parts = [f'"action": "{action}"']
    parts.extend(f'"{key}": "{value}"' for key, value in fields.items())
    return "{" + ", ".join(parts) + "}"


def _scripted_model(*actions: str) -> ModelGateway:
    return scripted_gateway(actions)


def _refund_setup_script(*, order_id: str, amount: str) -> list[str]:
    return [
        _action_json("get_orders"),
        _action_json("link_order", order_id=order_id),
        _action_json("propose_refund", amount=amount),
    ]


def _finish_script(
    *,
    reply_subject: str = "Refund update",
    reply_body: str = "Your refund request has been handled.",
    summary: str = "Refund lifecycle completed.",
) -> list[str]:
    return [
        _action_json("send_reply", subject=reply_subject, body=reply_body),
        _action_json("finish", summary=summary),
    ]


async def _post_inbound_email(
    client: AsyncClient,
    *,
    message_id: str,
    subject: str = "Refund request",
    body: str = "Please refund my order.",
) -> dict[str, Any]:
    response = await client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": message_id,
            "subject": subject,
            "body": body,
        },
    )
    assert response.status_code == 200
    return cast("dict[str, Any]", response.json())


async def _load_case_context(
    session_factory: async_sessionmaker[AsyncSession],
    case_id: str,
) -> tuple[list[Any], Any]:
    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )
    return case_events, case_summary


def _assert_event_sequence(
    case_events: list[Any],
    *,
    expected_types: list[EventType],
) -> None:
    actual_types = [event.event_type for event in case_events]
    assert actual_types == list(expected_types)


def _assert_actor_channels(
    case_events: list[Any],
    *,
    expectations: list[tuple[EventType, Actor, Channel]],
) -> None:
    event_by_type = {event.event_type: event for event in case_events}
    for event_type, actor, channel in expectations:
        event = event_by_type[event_type]
        assert event.actor is actor
        assert event.channel is channel


async def _assert_refund_row(
    session_factory: async_sessionmaker[AsyncSession],
    refund_id: str,
    *,
    status: RefundStatus,
    amount: Decimal,
    order_id: str,
) -> None:
    async with session_factory() as session:
        refund_row = await find_refund_by_id(session, refund_id)
    assert refund_row is not None
    assert refund_row.status is status
    assert refund_row.amount == amount
    assert refund_row.order_id == order_id


async def test_happy_path_small_refund(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 1: Sophie, ORD-1001, 249.00 auto-approves and closes the case."""
    script = _refund_setup_script(
        order_id=ORD_1001_ID, amount="249.00"
    ) + _finish_script(
        reply_subject="Refund processed",
        reply_body="Your refund has been processed.",
        summary="Small refund completed.",
    )
    app = create_app(
        session_factory=api_session_factory, model_gateway=_scripted_model(*script)
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-happy",
            subject="Refund",
            body="My espresso machine arrived damaged.",
        )

    assert inbound["status"] == CaseStatus.CLOSED.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)

    _assert_event_sequence(
        case_events,
        expected_types=[
            EventType.CASE_OPENED,
            EventType.EMAIL_RECEIVED,
            EventType.ORDERS_LISTED,
            EventType.ORDER_LINKED,
            EventType.REFUND_PROPOSED,
            EventType.REFUND_AUTO_APPROVED,
            EventType.REFUND_EXECUTED,
            EventType.REPLY_SENT,
            EventType.CASE_CLOSED,
        ],
    )
    _assert_actor_channels(
        case_events,
        expectations=[
            (EventType.CASE_OPENED, Actor.SYSTEM, Channel.INTERNAL),
            (EventType.EMAIL_RECEIVED, Actor.CUSTOMER, Channel.EMAIL),
            (EventType.ORDERS_LISTED, Actor.AGENT, Channel.INTERNAL),
            (EventType.ORDER_LINKED, Actor.AGENT, Channel.INTERNAL),
            (EventType.REFUND_PROPOSED, Actor.AGENT, Channel.INTERNAL),
            (EventType.REFUND_AUTO_APPROVED, Actor.SYSTEM, Channel.INTERNAL),
            (EventType.REFUND_EXECUTED, Actor.SYSTEM, Channel.INTERNAL),
            (EventType.REPLY_SENT, Actor.AGENT, Channel.INTERNAL),
            (EventType.CASE_CLOSED, Actor.SYSTEM, Channel.INTERNAL),
        ],
    )

    email_received = EmailReceivedPayload.model_validate(case_events[1].payload)
    assert email_received.message_id == "msg-lifecycle-happy"
    orders_listed = OrdersListedPayload.model_validate(case_events[2].payload)
    assert ORD_1001_ID in orders_listed.order_ids
    order_linked = OrderLinkedPayload.model_validate(case_events[3].payload)
    assert order_linked.order_id == ORD_1001_ID

    proposed = RefundProposedPayload.model_validate(case_events[4].payload)
    assert proposed.amount == Decimal("249.00")
    auto_approved = RefundAutoApprovedPayload.model_validate(case_events[5].payload)
    assert auto_approved.refund_id == proposed.refund_id
    executed = RefundExecutedPayload.model_validate(case_events[6].payload)
    assert executed.refund_id == proposed.refund_id
    assert executed.amount == Decimal("249.00")
    assert executed.provider_ref == f"pay_{proposed.refund_id}"

    reply_sent = ReplySentPayload.model_validate(case_events[7].payload)
    assert reply_sent.subject == "Refund processed"
    case_closed = CaseClosedPayload.model_validate(case_events[8].payload)
    assert case_closed.outcome is CaseOutcome.FINISHED
    assert case_closed.summary == "Small refund completed."

    await _assert_refund_row(
        api_session_factory,
        proposed.refund_id,
        status=RefundStatus.EXECUTED,
        amount=Decimal("249.00"),
        order_id=ORD_1001_ID,
    )
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=proposed.refund_id,
            amount=Decimal("249.00"),
        ),
    ]
    assert case_summary.status is CaseStatus.CLOSED


async def test_large_refund_requires_approval(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 2: Sophie, ORD-1003, 780.00 suspends then completes after approval."""
    inbound_script = _refund_setup_script(order_id=ORD_1003_ID, amount="780.00")
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*inbound_script),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-large",
            body="The grinder stopped working.",
        )

    assert inbound["status"] == CaseStatus.AWAITING_APPROVAL.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)

    _assert_event_sequence(
        case_events,
        expected_types=[
            EventType.CASE_OPENED,
            EventType.EMAIL_RECEIVED,
            EventType.ORDERS_LISTED,
            EventType.ORDER_LINKED,
            EventType.REFUND_PROPOSED,
            EventType.REFUND_APPROVAL_REQUIRED,
        ],
    )
    _assert_actor_channels(
        case_events,
        expectations=[
            (EventType.REFUND_PROPOSED, Actor.AGENT, Channel.INTERNAL),
            (EventType.REFUND_APPROVAL_REQUIRED, Actor.SYSTEM, Channel.INTERNAL),
        ],
    )

    approval_required = RefundApprovalRequiredPayload.model_validate(
        case_events[-1].payload,
    )
    assert approval_required.amount == Decimal("780.00")
    assert approval_required.rule == "R_THRESHOLD"
    await _assert_refund_row(
        api_session_factory,
        approval_required.refund_id,
        status=RefundStatus.PENDING_APPROVAL,
        amount=Decimal("780.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == []
    assert case_summary.status is CaseStatus.AWAITING_APPROVAL
    assert case_summary.last_refund_status is RefundStatus.PENDING_APPROVAL

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(
            *_finish_script(
                reply_subject="Refund approved",
                reply_body="Your refund has been approved.",
                summary="Large refund approved and completed.",
            )
        ),
    )
    resume_transport = ASGITransport(app=resume_app)
    async with AsyncClient(
        transport=resume_transport,
        base_url="http://test",
    ) as client:
        approve_response = await client.post(
            "/operator/approve",
            json={
                "refund_id": approval_required.refund_id,
                "operator_id": "op-lifecycle-1",
            },
        )

    assert approve_response.status_code == 200
    approve_body = approve_response.json()
    assert approve_body["refund_status"] == RefundStatus.EXECUTED.value
    assert approve_body["case_id"] == case_id

    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    _assert_event_sequence(
        case_events,
        expected_types=[
            EventType.CASE_OPENED,
            EventType.EMAIL_RECEIVED,
            EventType.ORDERS_LISTED,
            EventType.ORDER_LINKED,
            EventType.REFUND_PROPOSED,
            EventType.REFUND_APPROVAL_REQUIRED,
            EventType.REFUND_APPROVED,
            EventType.REFUND_EXECUTED,
            EventType.REPLY_SENT,
            EventType.CASE_CLOSED,
        ],
    )
    _assert_actor_channels(
        case_events,
        expectations=[
            (EventType.REFUND_APPROVED, Actor.OPERATOR, Channel.OPERATOR_API),
            (EventType.REFUND_EXECUTED, Actor.SYSTEM, Channel.INTERNAL),
            (EventType.REPLY_SENT, Actor.AGENT, Channel.INTERNAL),
            (EventType.CASE_CLOSED, Actor.SYSTEM, Channel.INTERNAL),
        ],
    )

    approved = RefundApprovedPayload.model_validate(case_events[-4].payload)
    assert approved.refund_id == approval_required.refund_id
    assert approved.operator_id == "op-lifecycle-1"
    executed = RefundExecutedPayload.model_validate(case_events[-3].payload)
    assert executed.refund_id == approval_required.refund_id
    assert executed.amount == Decimal("780.00")
    case_closed = CaseClosedPayload.model_validate(case_events[-1].payload)
    assert case_closed.outcome is CaseOutcome.FINISHED

    await _assert_refund_row(
        api_session_factory,
        approval_required.refund_id,
        status=RefundStatus.EXECUTED,
        amount=Decimal("780.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=approval_required.refund_id,
            amount=Decimal("780.00"),
        ),
    ]
    assert case_summary.status is CaseStatus.CLOSED


async def test_operator_rejection(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 3: large refund rejection resumes the case without payment."""
    inbound_script = _refund_setup_script(order_id=ORD_1003_ID, amount="780.00")
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*inbound_script),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-reject",
            body="Please refund the grinder.",
        )

    approval_required = RefundApprovalRequiredPayload.model_validate(
        (await _load_case_context(api_session_factory, inbound["case_id"]))[0][
            -1
        ].payload,
    )
    assert payment.calls == []

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(
            *_finish_script(
                reply_subject="Refund update",
                reply_body="Your refund request was not approved.",
                summary="Rejected refund completed.",
            )
        ),
    )
    resume_transport = ASGITransport(app=resume_app)
    async with AsyncClient(
        transport=resume_transport,
        base_url="http://test",
    ) as client:
        reject_response = await client.post(
            "/operator/reject",
            json={
                "refund_id": approval_required.refund_id,
                "operator_id": "op-lifecycle-2",
                "reason": "not justified",
            },
        )

    assert reject_response.status_code == 200
    assert reject_response.json()["refund_status"] == RefundStatus.REJECTED.value

    case_events, case_summary = await _load_case_context(
        api_session_factory,
        inbound["case_id"],
    )
    _assert_event_sequence(
        case_events,
        expected_types=[
            EventType.CASE_OPENED,
            EventType.EMAIL_RECEIVED,
            EventType.ORDERS_LISTED,
            EventType.ORDER_LINKED,
            EventType.REFUND_PROPOSED,
            EventType.REFUND_APPROVAL_REQUIRED,
            EventType.REFUND_REJECTED,
            EventType.REPLY_SENT,
            EventType.CASE_CLOSED,
        ],
    )
    _assert_actor_channels(
        case_events,
        expectations=[
            (EventType.REFUND_REJECTED, Actor.OPERATOR, Channel.OPERATOR_API),
            (EventType.REPLY_SENT, Actor.AGENT, Channel.INTERNAL),
            (EventType.CASE_CLOSED, Actor.SYSTEM, Channel.INTERNAL),
        ],
    )

    rejected = RefundRejectedPayload.model_validate(case_events[-3].payload)
    assert rejected.refund_id == approval_required.refund_id
    assert rejected.operator_id == "op-lifecycle-2"
    assert rejected.reason == "not justified"
    case_closed = CaseClosedPayload.model_validate(case_events[-1].payload)
    assert case_closed.outcome is CaseOutcome.FINISHED

    await _assert_refund_row(
        api_session_factory,
        approval_required.refund_id,
        status=RefundStatus.REJECTED,
        amount=Decimal("780.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == []
    assert case_summary.status is CaseStatus.CLOSED


async def test_approval_does_not_carry_over(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 4: second proposal still needs approval after first executes."""
    inbound_script = _refund_setup_script(order_id=ORD_1003_ID, amount="600.00")
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*inbound_script),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-carryover",
            body="Partial refund please.",
        )

    case_id = inbound["case_id"]
    first_approval = RefundApprovalRequiredPayload.model_validate(
        (await _load_case_context(api_session_factory, case_id))[0][-1].payload,
    )

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(_action_json("propose_refund", amount="100.00")),
    )
    resume_transport = ASGITransport(app=resume_app)
    async with AsyncClient(
        transport=resume_transport,
        base_url="http://test",
    ) as client:
        approve_response = await client.post(
            "/operator/approve",
            json={
                "refund_id": first_approval.refund_id,
                "operator_id": "op-lifecycle-3",
            },
        )

    assert approve_response.status_code == 200
    assert approve_response.json()["refund_status"] == RefundStatus.EXECUTED.value

    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert case_events[-2].event_type is EventType.REFUND_PROPOSED
    second_proposed = RefundProposedPayload.model_validate(case_events[-2].payload)
    second_approval = RefundApprovalRequiredPayload.model_validate(
        case_events[-1].payload
    )

    assert second_proposed.amount == Decimal("100.00")
    assert second_approval.refund_id == second_proposed.refund_id
    assert second_approval.rule == "R_THRESHOLD"
    assert second_approval.amount == Decimal("100.00")
    assert second_approval.refund_id != first_approval.refund_id

    await _assert_refund_row(
        api_session_factory,
        first_approval.refund_id,
        status=RefundStatus.EXECUTED,
        amount=Decimal("600.00"),
        order_id=ORD_1003_ID,
    )
    await _assert_refund_row(
        api_session_factory,
        second_approval.refund_id,
        status=RefundStatus.PENDING_APPROVAL,
        amount=Decimal("100.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=first_approval.refund_id,
            amount=Decimal("600.00"),
        ),
    ]
    assert case_summary.status is CaseStatus.AWAITING_APPROVAL
    assert case_summary.last_refund_status is RefundStatus.PENDING_APPROVAL


async def test_cumulative_threshold_survives_new_case_on_same_order(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Executed refunds in one case still count toward threshold in the next case."""
    first_case_script = [
        *_refund_setup_script(order_id=ORD_1003_ID, amount="300.00"),
        *_finish_script(summary="First case partial refund completed."),
    ]
    second_case_script = [
        *_refund_setup_script(order_id=ORD_1003_ID, amount="300.00"),
    ]
    first_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*first_case_script),
    )
    second_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*second_case_script),
    )

    async with AsyncClient(
        transport=ASGITransport(app=first_app),
        base_url="http://test",
    ) as client:
        first_inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-cumulative-case-one",
            body="First case partial refund please.",
        )

    assert first_inbound["status"] == CaseStatus.CLOSED.value
    first_case_id = first_inbound["case_id"]

    async with AsyncClient(
        transport=ASGITransport(app=second_app),
        base_url="http://test",
    ) as client:
        second_inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-cumulative-case-two",
            body="Second case partial refund on the same order.",
        )

    assert second_inbound["status"] == CaseStatus.AWAITING_APPROVAL.value
    second_case_id = second_inbound["case_id"]
    assert second_case_id != first_case_id

    first_events, _ = await _load_case_context(api_session_factory, first_case_id)
    second_events, second_summary = await _load_case_context(
        api_session_factory,
        second_case_id,
    )

    assert [event.event_type for event in first_events] == [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_AUTO_APPROVED,
        EventType.REFUND_EXECUTED,
        EventType.REPLY_SENT,
        EventType.CASE_CLOSED,
    ]
    assert [event.event_type for event in second_events] == [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_APPROVAL_REQUIRED,
    ]

    first_executed = RefundExecutedPayload.model_validate(first_events[6].payload)
    second_proposed = RefundProposedPayload.model_validate(second_events[4].payload)
    second_approval = RefundApprovalRequiredPayload.model_validate(
        second_events[5].payload,
    )
    assert second_approval.refund_id == second_proposed.refund_id
    assert second_approval.rule == "R_THRESHOLD"
    assert second_approval.amount == Decimal("300.00")
    assert second_approval.refund_id != first_executed.refund_id

    await _assert_refund_row(
        api_session_factory,
        first_executed.refund_id,
        status=RefundStatus.EXECUTED,
        amount=Decimal("300.00"),
        order_id=ORD_1003_ID,
    )
    await _assert_refund_row(
        api_session_factory,
        second_proposed.refund_id,
        status=RefundStatus.PENDING_APPROVAL,
        amount=Decimal("300.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=first_executed.refund_id,
            amount=Decimal("300.00"),
        ),
    ]
    assert second_summary.status is CaseStatus.AWAITING_APPROVAL
    assert second_summary.last_refund_status is RefundStatus.PENDING_APPROVAL


async def test_cumulative_threshold(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 5: two 300.00 proposals on ORD-1003 trigger threshold on the second."""
    script = [
        *_refund_setup_script(order_id=ORD_1003_ID, amount="300.00"),
        _action_json("propose_refund", amount="300.00"),
    ]
    app = create_app(
        session_factory=api_session_factory, model_gateway=_scripted_model(*script)
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-cumulative",
            body="Two partial refunds please.",
        )

    assert inbound["status"] == CaseStatus.AWAITING_APPROVAL.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)

    event_types = [event.event_type for event in case_events]
    assert event_types == [
        EventType.CASE_OPENED,
        EventType.EMAIL_RECEIVED,
        EventType.ORDERS_LISTED,
        EventType.ORDER_LINKED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_AUTO_APPROVED,
        EventType.REFUND_EXECUTED,
        EventType.REFUND_PROPOSED,
        EventType.REFUND_APPROVAL_REQUIRED,
    ]

    first_proposed = RefundProposedPayload.model_validate(case_events[4].payload)
    first_executed = RefundExecutedPayload.model_validate(case_events[6].payload)
    second_proposed = RefundProposedPayload.model_validate(case_events[7].payload)
    second_approval = RefundApprovalRequiredPayload.model_validate(
        case_events[8].payload
    )

    assert first_executed.refund_id == first_proposed.refund_id
    assert first_executed.amount == Decimal("300.00")
    assert second_approval.refund_id == second_proposed.refund_id
    assert second_approval.amount == Decimal("300.00")
    assert second_approval.rule == "R_THRESHOLD"

    await _assert_refund_row(
        api_session_factory,
        first_proposed.refund_id,
        status=RefundStatus.EXECUTED,
        amount=Decimal("300.00"),
        order_id=ORD_1003_ID,
    )
    await _assert_refund_row(
        api_session_factory,
        second_proposed.refund_id,
        status=RefundStatus.PENDING_APPROVAL,
        amount=Decimal("300.00"),
        order_id=ORD_1003_ID,
    )
    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=first_proposed.refund_id,
            amount=Decimal("300.00"),
        ),
    ]
    assert case_summary.status is CaseStatus.AWAITING_APPROVAL
    assert case_summary.last_refund_status is RefundStatus.PENDING_APPROVAL


async def test_approval_payment_matches_proposal_row_and_event(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Approval and payment must match refund_proposed evidence and the row."""
    inbound_script = _refund_setup_script(order_id=ORD_1003_ID, amount="780.00")
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*inbound_script),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-lifecycle-intent-binding",
            body="The grinder stopped working.",
        )

    case_id = inbound["case_id"]
    case_events, _ = await _load_case_context(api_session_factory, case_id)

    proposed_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_PROPOSED
    )
    approval_required_event = next(
        event
        for event in case_events
        if event.event_type is EventType.REFUND_APPROVAL_REQUIRED
    )
    proposed = RefundProposedPayload.model_validate(proposed_event.payload)
    approval_required = RefundApprovalRequiredPayload.model_validate(
        approval_required_event.payload,
    )
    assert approval_required.refund_id == proposed.refund_id
    assert approval_required.amount == proposed.amount
    for event in (proposed_event, approval_required_event):
        assert event.customer_id == SOPHIE_CUSTOMER_ID
        assert event.case_id == case_id
        assert event.order_id == ORD_1003_ID

    async with api_session_factory() as session:
        refund_row = await find_refund_by_id(session, proposed.refund_id)
    assert refund_row is not None
    assert refund_row.id == proposed.refund_id
    assert refund_row.amount == proposed.amount
    assert refund_row.order_id == ORD_1003_ID
    assert refund_row.case_id == case_id

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(
            *_finish_script(
                reply_subject="Refund approved",
                reply_body="Your refund has been approved.",
                summary="Intent-bound refund completed.",
            )
        ),
    )
    resume_transport = ASGITransport(app=resume_app)
    async with AsyncClient(
        transport=resume_transport,
        base_url="http://test",
    ) as client:
        approve_response = await client.post(
            "/operator/approve",
            json={
                "refund_id": proposed.refund_id,
                "operator_id": "op-intent-binding",
            },
        )

    assert approve_response.status_code == 200

    case_events, _ = await _load_case_context(api_session_factory, case_id)
    approved_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_APPROVED
    )
    executed_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_EXECUTED
    )
    approved = RefundApprovedPayload.model_validate(approved_event.payload)
    executed = RefundExecutedPayload.model_validate(executed_event.payload)

    assert approved.refund_id == proposed.refund_id
    assert executed.refund_id == proposed.refund_id
    assert executed.amount == proposed.amount
    for event in (
        proposed_event,
        approval_required_event,
        approved_event,
        executed_event,
    ):
        assert event.customer_id == SOPHIE_CUSTOMER_ID
        assert event.case_id == case_id
        assert event.order_id == ORD_1003_ID

    async with api_session_factory() as session:
        refund_row = await find_refund_by_id(session, proposed.refund_id)
    assert refund_row is not None
    assert refund_row.amount == proposed.amount
    assert refund_row.order_id == ORD_1003_ID
    assert refund_row.case_id == case_id

    assert payment.calls == [
        payment.RefundCall(
            idempotency_key=proposed.refund_id,
            amount=proposed.amount,
        ),
    ]
