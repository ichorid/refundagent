"""Integration scenario 16: approval expiry via lazy housekeeping."""

from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.adapters import payment
from saferefund.domain.enums import (
    Actor,
    CaseStatus,
    Channel,
    RefundStatus,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    RefundApprovalRequiredPayload,
    RefundExpiredPayload,
)
from saferefund.domain.tables import CaseRow, RefundRow
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import (
    ORD_1003_ID,
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    TOM_CUSTOMER_ID,
)
from tests.conftest import FIXED_TEST_NOW
from tests.invariants.scenario import propose_refund_awaiting_approval
from tests.support.model_gateway import scripted_gateway
from tests.support.sequence_assertions import (
    GATE_PENDING_APPROVAL_SEQUENCE,
    INBOUND_PENDING_APPROVAL_SEQUENCE,
    assert_case_expired_with_agent_resume,
    assert_exact_event_type_sequence,
)


def _action_json(action: str, **fields: str) -> str:
    parts = [f'"action": "{action}"']
    parts.extend(f'"{key}": "{value}"' for key, value in fields.items())
    return "{" + ", ".join(parts) + "}"


def _refund_setup_script(*, order_id: str, amount: str) -> list[str]:
    return [
        _action_json("get_orders"),
        _action_json("link_order", order_id=order_id),
        _action_json("propose_refund", amount=amount),
    ]


async def _post_inbound_email(
    client: AsyncClient,
    *,
    message_id: str,
    body: str = "Please refund my order.",
) -> dict[str, Any]:
    response = await client.post(
        "/inbound-email",
        json={
            "envelope_from": SOPHIE_EMAIL,
            "message_id": message_id,
            "subject": "Refund request",
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
            now=clock.now(),
        )
    return case_events, case_summary


EXPIRY_INSTANT = FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS)

ORDINARY_TRAFFIC_CUSTOMER_EVENT_SEQUENCE: tuple[EventType, ...] = (
    EventType.CUSTOMER_VERIFIED,
    *GATE_PENDING_APPROVAL_SEQUENCE,
    EventType.REFUND_EXPIRED,
    EventType.CASE_OPENED,
    EventType.EMAIL_RECEIVED,
    EventType.CASE_CLOSED,
    EventType.ORDERS_LISTED,
    EventType.ORDER_LINKED,
    EventType.REFUND_PROPOSED,
    EventType.REFUND_APPROVAL_REQUIRED,
)

CUSTOMER_SCOPED_SOPHIE_CUSTOMER_EVENT_SEQUENCE: tuple[EventType, ...] = (
    EventType.CUSTOMER_VERIFIED,
    *GATE_PENDING_APPROVAL_SEQUENCE,
    EventType.REFUND_EXPIRED,
    EventType.CASE_OPENED,
    EventType.EMAIL_RECEIVED,
    EventType.CASE_CLOSED,
    EventType.ESCALATED,
    EventType.CASE_CLOSED,
)


def _advance_clock_past_approval_ttl() -> None:
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )


def _advance_clock_to_expiry_instant() -> None:
    clock.set_now_for_tests(EXPIRY_INSTANT)


async def _create_pending_large_refund(
    api_session_factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
) -> tuple[str, str]:
    """Return case_id and refund_id for scenario-2 large refund awaiting approval."""
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            _refund_setup_script(order_id=ORD_1003_ID, amount="780.00")
        ),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(client, message_id=message_id)

    assert inbound["status"] == CaseStatus.AWAITING_APPROVAL.value
    case_id = inbound["case_id"]
    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_exact_event_type_sequence(
        case_events,
        INBOUND_PENDING_APPROVAL_SEQUENCE,
    )
    assert case_summary.status is CaseStatus.AWAITING_APPROVAL
    assert case_summary.last_refund_status is RefundStatus.PENDING_APPROVAL
    approval_required = RefundApprovalRequiredPayload.model_validate(
        case_events[-1].payload,
    )
    return case_id, approval_required.refund_id


async def test_approval_expires(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 16: expired approval returns 409 and reopens the case."""
    case_id, refund_id = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-approval-expiry",
    )
    _advance_clock_past_approval_ttl()

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [_action_json("finish", summary="Expired and closed.")]
        ),
    )
    transport = ASGITransport(app=resume_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        approve_response = await client.post(
            "/operator/approve",
            json={
                "refund_id": refund_id,
                "operator_id": "op-expired",
            },
        )

    assert approve_response.status_code == 409
    conflict_body = approve_response.json()
    assert conflict_body["detail"]["refund_id"] == refund_id
    assert conflict_body["detail"]["refund_status"] == RefundStatus.EXPIRED.value

    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_case_expired_with_agent_resume(
        case_events,
        pending_sequence=INBOUND_PENDING_APPROVAL_SEQUENCE,
        refund_id=refund_id,
    )

    expired_event = next(
        event for event in case_events if event.event_type is EventType.REFUND_EXPIRED
    )
    expired_payload = RefundExpiredPayload.model_validate(expired_event.payload)
    assert expired_payload.refund_id == refund_id
    assert expired_event.actor is Actor.SYSTEM
    assert expired_event.channel is Channel.INTERNAL

    async with api_session_factory() as session:
        refund_row = await find_refund_by_id(session, refund_id)
    assert refund_row is not None
    assert refund_row.status is RefundStatus.EXPIRED
    assert refund_row.approval_expires_at is None

    assert payment.calls == []
    assert case_summary.status is CaseStatus.CLOSED
    assert case_summary.last_refund_status is RefundStatus.EXPIRED


async def test_approve_at_exact_expiry_instant_returns_conflict_without_payment(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At exactly approval_expires_at the lifecycle refuses approval with expiry."""
    case_id, refund_id = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-approval-expiry-equality",
    )

    async with api_session_factory() as session:
        refund_row = await find_refund_by_id(session, refund_id)
    assert refund_row is not None
    stored_expiry = refund_row.approval_expires_at
    assert stored_expiry is not None
    # SQLite returns naive datetimes for DateTime(timezone=True) columns.
    assert stored_expiry.replace(tzinfo=UTC) == EXPIRY_INSTANT
    _advance_clock_to_expiry_instant()
    payment.calls.clear()

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [_action_json("finish", summary="Expired and closed.")]
        ),
    )
    transport = ASGITransport(app=resume_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        approve_response = await client.post(
            "/operator/approve",
            json={
                "refund_id": refund_id,
                "operator_id": "op-expiry-equality",
            },
        )

    assert approve_response.status_code == 409
    assert (
        approve_response.json()["detail"]["refund_status"] == RefundStatus.EXPIRED.value
    )

    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_case_expired_with_agent_resume(
        case_events,
        pending_sequence=INBOUND_PENDING_APPROVAL_SEQUENCE,
        refund_id=refund_id,
    )
    assert payment.calls == []
    assert case_summary.status is CaseStatus.CLOSED
    assert case_summary.last_refund_status is RefundStatus.EXPIRED


async def test_reject_after_expiry_returns_conflict_without_rejection_event(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Reject on an expired refund appends refund_expired only, then returns 409."""
    case_id, refund_id = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-reject-after-expiry",
    )
    _advance_clock_past_approval_ttl()

    resume_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [_action_json("finish", summary="Expired and closed.")]
        ),
    )
    transport = ASGITransport(app=resume_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        reject_response = await client.post(
            "/operator/reject",
            json={
                "refund_id": refund_id,
                "operator_id": "op-expired-reject",
                "reason": "too late",
            },
        )

    assert reject_response.status_code == 409
    assert (
        reject_response.json()["detail"]["refund_status"] == RefundStatus.EXPIRED.value
    )

    case_events, case_summary = await _load_case_context(api_session_factory, case_id)
    assert_case_expired_with_agent_resume(
        case_events,
        pending_sequence=INBOUND_PENDING_APPROVAL_SEQUENCE,
        refund_id=refund_id,
    )
    assert case_summary.status is CaseStatus.CLOSED


OLD_CASE_ID = "case_ordinary_traffic_old"


async def test_expired_approval_is_reaped_by_ordinary_inbound_traffic(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unrelated inbound mail expires stale approvals and resumes the old case."""
    old_refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id=OLD_CASE_ID,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-ordinary-traffic-old",
    )
    _advance_clock_past_approval_ttl()

    traffic_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [
                _action_json("finish", summary="Old case closed after expiry."),
                _action_json("get_orders"),
                _action_json("link_order", order_id=ORD_1003_ID),
                _action_json("propose_refund", amount="780.00"),
            ]
        ),
    )
    transport = ASGITransport(app=traffic_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(
            client,
            message_id="msg-ordinary-traffic-new",
            body="Different question about another order.",
        )

    new_case_id = inbound["case_id"]
    assert new_case_id != OLD_CASE_ID

    old_case_events, old_case_summary = await _load_case_context(
        api_session_factory,
        OLD_CASE_ID,
    )
    assert_case_expired_with_agent_resume(
        old_case_events,
        pending_sequence=GATE_PENDING_APPROVAL_SEQUENCE,
        refund_id=old_refund_id,
    )
    assert old_case_summary.status is CaseStatus.CLOSED

    async with api_session_factory() as session:
        old_refund_row = await find_refund_by_id(session, old_refund_id)
    assert old_refund_row is not None
    assert old_refund_row.status is RefundStatus.EXPIRED

    new_case_events, new_case_summary = await _load_case_context(
        api_session_factory,
        new_case_id,
    )
    assert_exact_event_type_sequence(
        new_case_events,
        INBOUND_PENDING_APPROVAL_SEQUENCE,
    )
    assert new_case_summary.status is CaseStatus.AWAITING_APPROVAL
    assert not any(
        event.event_type is EventType.ACTION_DENIED for event in new_case_events
    )

    async with api_session_factory() as session:
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
    assert_exact_event_type_sequence(
        customer_events,
        ORDINARY_TRAFFIC_CUSTOMER_EVENT_SEQUENCE,
    )


async def test_customer_scoped_expiry_sweep_does_not_touch_other_customers(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sweeping one customer must not expire another customer's due refunds."""
    sophie_refund_id = await propose_refund_awaiting_approval(
        api_session_factory,
        case_id="case_sophie_scope",
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id="msg-sophie-scope",
    )
    tom_expiry = clock.now() + timedelta(seconds=config.APPROVAL_TTL_SECONDS)
    async with api_session_factory.begin() as session:
        session.add(
            CaseRow(
                id="case_tom_scope",
                customer_id=TOM_CUSTOMER_ID,
                opening_message_id="msg-tom-scope",
                created_at=FIXED_TEST_NOW,
            )
        )
        session.add(
            RefundRow(
                id="rfnd_tom_scope",
                customer_id=TOM_CUSTOMER_ID,
                order_id=ORD_2001_ID,
                case_id="case_tom_scope",
                amount=Decimal("60.00"),
                status=RefundStatus.PENDING_APPROVAL,
                approval_expires_at=tom_expiry,
                created_at=FIXED_TEST_NOW,
            )
        )
    tom_refund_id = "rfnd_tom_scope"
    _advance_clock_past_approval_ttl()

    traffic_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            [_action_json("finish", summary="Sophie only.")]
        ),
    )
    transport = ASGITransport(app=traffic_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": SOPHIE_EMAIL,
                "message_id": "msg-sophie-scope-inbound",
                "subject": "Follow up",
                "body": "Any update?",
            },
        )
    assert response.status_code == 200

    async with api_session_factory() as session:
        sophie_refund = await find_refund_by_id(session, sophie_refund_id)
        tom_refund = await find_refund_by_id(session, tom_refund_id)
    assert sophie_refund is not None
    assert sophie_refund.status is RefundStatus.EXPIRED
    assert tom_refund is not None
    assert tom_refund.status is RefundStatus.PENDING_APPROVAL
    assert tom_refund.approval_expires_at is not None

    sophie_case_events, _ = await _load_case_context(
        api_session_factory,
        "case_sophie_scope",
    )
    assert_case_expired_with_agent_resume(
        sophie_case_events,
        pending_sequence=GATE_PENDING_APPROVAL_SEQUENCE,
        refund_id=sophie_refund_id,
    )

    async with api_session_factory() as session:
        tom_case_events = await load_case_events(session, "case_tom_scope")
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
    assert_exact_event_type_sequence(tom_case_events, [])
    assert_exact_event_type_sequence(
        customer_events,
        CUSTOMER_SCOPED_SOPHIE_CUSTOMER_EVENT_SEQUENCE,
    )
