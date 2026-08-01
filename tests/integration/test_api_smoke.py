"""HTTP smoke tests for every public route status code."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import GetOrders, LinkOrder, ProposeRefund
from saferefund.adapters import mailer, payment
from saferefund.domain.enums import Actor, CaseStatus, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.domain.tables import CaseRow, EventRow
from saferefund.gate.operations import (
    approve_refund,
    execute_agent_action,
    reject_refund,
)
from saferefund.main import create_app
from saferefund.policy.verdicts import RequireApproval
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import (
    ORD_1003_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    TOM_CUSTOMER_ID,
    TOM_EMAIL,
    UNKNOWN_SENDER_EMAIL,
)
from tests.support.model_gateway import heuristic_gateway, scripted_gateway
from tests.support.sequence_assertions import (
    OPERATOR_APPROVE_CONFLICT_AFTER_REJECT_SEQUENCE,
    OPERATOR_REJECT_CONFLICT_AFTER_APPROVE_SEQUENCE,
    assert_operator_approve_response_lifecycle,
    assert_operator_conflict_response_unchanged,
    assert_operator_reject_response_lifecycle,
)


async def _open_case(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
    created_at: datetime,
) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=created_at,
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


async def _create_pending_refund(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_id: str,
    customer_id: str,
    opening_message_id: str,
) -> str:
    async with session_factory.begin() as session:
        await _open_case(
            session,
            case_id=case_id,
            customer_id=customer_id,
            opening_message_id=opening_message_id,
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
        await execute_agent_action(
            session,
            case_id,
            GetOrders(action="get_orders"),
        )
        await execute_agent_action(
            session,
            case_id,
            LinkOrder(action="link_order", order_id=ORD_1003_ID),
        )
        verdict = await execute_agent_action(
            session,
            case_id,
            ProposeRefund(action="propose_refund", amount=Decimal("780.00")),
        )
    assert isinstance(verdict, RequireApproval)

    async with session_factory() as session:
        case_events = await load_case_events(session, case_id)
        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        return approval_required.refund_id


_PENDING_REFUND_AMOUNT = Decimal("780.00")


async def test_unknown_sender_returns_202_without_events(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown senders receive a canned reply and never create cases or events."""
    response = await api_client.post(
        "/inbound-email",
        json={
            "envelope_from": UNKNOWN_SENDER_EMAIL,
            "message_id": "msg-unknown-1",
            "subject": "Hello",
            "body": "Who are you?",
        },
    )

    assert response.status_code == 202
    assert response.json() == {"handled": "unknown_sender"}
    assert len(mailer.outbox) == 1
    assert mailer.outbox[0].to == UNKNOWN_SENDER_EMAIL

    async with api_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(EventRow))
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        assert event_count == 1
        assert case_count == 0


async def test_inbound_email_known_sender_returns_case_and_event_sequence(
    api_client: AsyncClient,
) -> None:
    """A known sender opens a case, runs the loop, and returns the event sequence."""
    response = await api_client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": "msg-sophie-smoke-1",
            "subject": "Refund please",
            "body": "My espresso machine arrived damaged.",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["case_id"].startswith("case_")
    assert body["status"] == CaseStatus.CLOSED.value
    event_types = [event["type"] for event in body["events"]]
    assert event_types == [
        EventType.CASE_OPENED.value,
        EventType.EMAIL_RECEIVED.value,
        EventType.ORDERS_LISTED.value,
        EventType.ORDER_LINKED.value,
        EventType.REFUND_PROPOSED.value,
        EventType.REFUND_AUTO_APPROVED.value,
        EventType.REFUND_EXECUTED.value,
        EventType.REPLY_SENT.value,
        EventType.CASE_CLOSED.value,
    ]
    assert all("seq" in event for event in body["events"])


async def test_duplicate_message_id_appends_no_second_email_received(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Duplicate opening message ids are idempotent and do not append email_received."""
    payload = {
        "envelope_from": SOPHIE_EMAIL,
        "message_id": "msg-sophie-dedupe",
        "subject": "Refund please",
        "body": "First message",
    }
    first_response = await api_client.post("/inbound-email", json=payload)
    assert first_response.status_code == 200
    first_body = first_response.json()
    first_event_count = len(first_body["events"])

    second_response = await api_client.post(
        "/inbound-email",
        json={
            **payload,
            "subject": "Changed subject",
            "body": "Changed body",
        },
    )
    assert second_response.status_code == 200
    second_body = second_response.json()
    assert second_body["case_id"] == first_body["case_id"]
    assert len(second_body["events"]) == first_event_count
    assert [event["type"] for event in second_body["events"]].count(
        EventType.EMAIL_RECEIVED.value
    ) == 1

    async with api_session_factory() as session:
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        assert case_count == 1


async def test_operator_approve_success_returns_200(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator approval resumes the owning case after payment."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_approve",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-approve",
    )
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [
                '{"action": "send_reply", "subject": "Done", "body": "Approved."}',
                '{"action": "finish", "summary": "Approved refund completed."}',
            ]
        ),
    )

    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/approve",
            json={"refund_id": refund_id, "operator_id": "op-smoke-1"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == refund_id
    assert body["refund_status"] == RefundStatus.EXECUTED.value
    assert body["case_id"] == "case_api_approve"


async def test_operator_approve_response_matches_exact_effect_and_event_sequence(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator approval must emit the exact lifecycle sequence and one payment call."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_approve_exact",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-approve-exact",
    )
    operator_id = "op-smoke-exact-approve"
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [
                '{"action": "send_reply", "subject": "Done", "body": "Approved."}',
                '{"action": "finish", "summary": "Approved refund completed."}',
            ]
        ),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/approve",
            json={"refund_id": refund_id, "operator_id": operator_id},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == refund_id
    assert body["refund_status"] == RefundStatus.EXECUTED.value
    assert body["case_id"] == "case_api_approve_exact"

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case_api_approve_exact")
        refund_row = await find_refund_by_id(session, refund_id)

    assert_operator_approve_response_lifecycle(
        case_events,
        refund_id=refund_id,
        amount=_PENDING_REFUND_AMOUNT,
        operator_id=operator_id,
        order_id=ORD_1003_ID,
        mailer_messages=[
            mailer.OutboxMessage(
                to=SOPHIE_EMAIL,
                subject="Done",
                body="Approved.",
            ),
        ],
        refund_row=refund_row,
    )


async def test_operator_approve_conflict_returns_409(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Approving a non-pending refund returns 409 with the current refund status."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_conflict",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-conflict",
    )

    async with api_session_factory.begin() as session:
        await reject_refund(
            session,
            refund_id,
            "op-setup",
            "setup rejection for conflict test",
        )

    response = await api_client.post(
        "/operator/approve",
        json={"refund_id": refund_id, "operator_id": "op-smoke-2"},
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["refund_id"] == refund_id
    assert detail["refund_status"] == RefundStatus.REJECTED.value

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case_api_conflict")
        refund_row = await find_refund_by_id(session, refund_id)

    assert_operator_conflict_response_unchanged(
        case_events,
        expected_types=OPERATOR_APPROVE_CONFLICT_AFTER_REJECT_SEQUENCE,
        payment_calls=[],
    )
    assert refund_row is not None
    assert refund_row.status is RefundStatus.REJECTED
    assert refund_row.id == refund_id
    assert refund_row.amount == _PENDING_REFUND_AMOUNT
    assert refund_row.order_id == ORD_1003_ID


async def test_operator_reject_success_returns_200(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator rejection resumes the owning case without payment."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_reject",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-reject",
    )
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [
                '{"action": "send_reply", "subject": "Update", "body": "Rejected."}',
                '{"action": "finish", "summary": "Rejected refund completed."}',
            ]
        ),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/reject",
            json={
                "refund_id": refund_id,
                "operator_id": "op-smoke-3",
                "reason": "not justified",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == refund_id
    assert body["refund_status"] == RefundStatus.REJECTED.value
    assert body["case_id"] == "case_api_reject"


async def test_operator_reject_response_matches_exact_no_payment_sequence(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Operator rejection must complete the case without payment or execution events."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_reject_exact",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-reject-exact",
    )
    operator_id = "op-smoke-exact-reject"
    reject_reason = "not justified"
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [
                '{"action": "send_reply", "subject": "Update", "body": "Rejected."}',
                '{"action": "finish", "summary": "Rejected refund completed."}',
            ]
        ),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/operator/reject",
            json={
                "refund_id": refund_id,
                "operator_id": operator_id,
                "reason": reject_reason,
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["refund_id"] == refund_id
    assert body["refund_status"] == RefundStatus.REJECTED.value
    assert body["case_id"] == "case_api_reject_exact"

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case_api_reject_exact")
        refund_row = await find_refund_by_id(session, refund_id)

    assert_operator_reject_response_lifecycle(
        case_events,
        refund_id=refund_id,
        amount=_PENDING_REFUND_AMOUNT,
        operator_id=operator_id,
        order_id=ORD_1003_ID,
        reason=reject_reason,
        mailer_messages=[
            mailer.OutboxMessage(
                to=SOPHIE_EMAIL,
                subject="Update",
                body="Rejected.",
            ),
        ],
        refund_row=refund_row,
    )


async def test_operator_reject_conflict_returns_409(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rejecting a non-pending refund returns 409 with the current refund status."""
    refund_id = await _create_pending_refund(
        api_session_factory,
        case_id="case_api_reject_conflict",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-api-reject-conflict",
    )

    async with api_session_factory.begin() as session:
        await approve_refund(
            session,
            refund_id,
            "op-setup",
            session_factory=api_session_factory,
        )

    response = await api_client.post(
        "/operator/reject",
        json={
            "refund_id": refund_id,
            "operator_id": "op-smoke-4",
            "reason": "too late",
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["refund_id"] == refund_id
    assert detail["refund_status"] == RefundStatus.EXECUTED.value

    async with api_session_factory() as session:
        case_events = await load_case_events(session, "case_api_reject_conflict")
        refund_row = await find_refund_by_id(session, refund_id)

    assert_operator_conflict_response_unchanged(
        case_events,
        expected_types=OPERATOR_REJECT_CONFLICT_AFTER_APPROVE_SEQUENCE,
        payment_calls=[
            payment.RefundCall(
                idempotency_key=refund_id,
                amount=_PENDING_REFUND_AMOUNT,
            ),
        ],
    )
    assert refund_row is not None
    assert refund_row.status is RefundStatus.EXECUTED
    assert refund_row.id == refund_id
    assert refund_row.amount == _PENDING_REFUND_AMOUNT
    assert refund_row.order_id == ORD_1003_ID


async def test_verification_confirm_unknown_token_returns_404(
    api_client: AsyncClient,
) -> None:
    """Unknown verification tokens return 404 without appending events."""
    response = await api_client.post(
        "/verification/confirm",
        json={"token": "vtok_missing"},
    )

    assert response.status_code == 404


async def test_verification_confirm_expired_token_returns_400_and_resumes_case(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Expired tokens return 400, append nothing, and resume the issuing case."""
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=heuristic_gateway(),
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": "msg-tom-expired-api",
                "subject": "Refund",
                "body": "Please help",
            },
        )
        assert inbound.status_code == 200
        case_id = inbound.json()["case_id"]

        async with api_session_factory() as session:
            case_events = await load_case_events(session, case_id)
            verification_event = next(
                event
                for event in case_events
                if event.event_type is EventType.VERIFICATION_REQUESTED
            )
            token = verification_event.payload["token"]

        clock.set_now_for_tests(
            datetime(2030, 1, 15, 9, 30, tzinfo=UTC)
            + timedelta(seconds=config.VERIFICATION_TTL_SECONDS + 1)
        )

        response = await client.post(
            "/verification/confirm",
            json={"token": token},
        )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["customer_id"] == TOM_CUSTOMER_ID
    assert detail["issuing_case_id"] == case_id


async def test_verification_confirm_valid_token_resumes_open_cases_oldest_first(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Valid verification resumes every open case for the customer oldest first."""
    verification_app = create_app(
        session_factory=api_session_factory,
        model_gateway=heuristic_gateway(),
    )
    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=heuristic_gateway(),
    )

    async with AsyncClient(
        transport=ASGITransport(app=verification_app),
        base_url="http://test",
    ) as client:
        first = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": "msg-tom-first-api",
                "subject": "Case one",
                "body": "First case",
            },
        )
        second = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": "msg-tom-second-api",
                "subject": "Case two",
                "body": "Second case",
            },
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_case_id = first.json()["case_id"]
    second_case_id = second.json()["case_id"]

    async with api_session_factory() as session:
        first_events = await load_case_events(session, first_case_id)
        verification_event = next(
            event
            for event in first_events
            if event.event_type is EventType.VERIFICATION_REQUESTED
        )
        token = verification_event.payload["token"]

    async with AsyncClient(
        transport=ASGITransport(app=resume_app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/verification/confirm",
            json={"token": token},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["customer_id"] == TOM_CUSTOMER_ID
    assert body["resumed_case_ids"] == [first_case_id, second_case_id]

    async with api_session_factory() as session:
        for case_id in body["resumed_case_ids"]:
            case_events = await load_case_events(session, case_id)
            assert case_events[-1].event_type is EventType.CASE_CLOSED


async def test_request_schemas_reject_actor_and_channel_fields(
    api_client: AsyncClient,
) -> None:
    """Trusted provenance fields are never accepted from HTTP request bodies."""
    response = await api_client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": "msg-forbidden-fields",
            "subject": "Hi",
            "body": "Body",
            "actor": "operator",
            "channel": "operator_api",
        },
    )

    assert response.status_code == 422
