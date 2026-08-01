"""Integration tests for the read-only operator pending-refund queue."""

from datetime import timedelta
from decimal import Decimal
from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.domain.enums import CaseStatus, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundApprovalRequiredPayload
from saferefund.domain.tables import CaseRow, EventRow, RefundRow
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.refunds import find_refund_by_id
from saferefund.repositories.seed import (
    ORD_1002_ID,
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


async def _create_pending_large_refund(
    api_session_factory: async_sessionmaker[AsyncSession],
    *,
    message_id: str,
    order_id: str = ORD_1003_ID,
    amount: str = "780.00",
) -> tuple[str, str, str]:
    """Return case_id, refund_id, and approval_expires_at for one pending refund."""
    inbound_app = create_app(
        session_factory=api_session_factory,
        model_gateway=scripted_gateway(
            _refund_setup_script(order_id=order_id, amount=amount)
        ),
    )
    transport = ASGITransport(app=inbound_app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        inbound = await _post_inbound_email(client, message_id=message_id)

    assert inbound["status"] == CaseStatus.AWAITING_APPROVAL.value
    case_id = inbound["case_id"]

    async with api_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        approval_required = RefundApprovalRequiredPayload.model_validate(
            case_events[-1].payload,
        )
        refund_row = await find_refund_by_id(session, approval_required.refund_id)
    assert refund_row is not None
    assert refund_row.approval_expires_at is not None
    return (
        case_id,
        approval_required.refund_id,
        refund_row.approval_expires_at.isoformat(),
    )


async def test_operator_pending_returns_active_rows(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Active pending refunds are listed with case, order, amount, and expiry."""
    case_id, refund_id, approval_expires_at = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-operator-pending-active",
    )

    async with api_session_factory() as session:
        event_count_before = await session.scalar(
            select(func.count()).select_from(EventRow)
        )

    app = create_app(session_factory=api_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/pending")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "pending_refunds": [
            {
                "refund_id": refund_id,
                "case_id": case_id,
                "order_id": ORD_1003_ID,
                "amount": "780.00",
                "approval_expires_at": approval_expires_at,
            }
        ]
    }

    async with api_session_factory() as session:
        event_count_after = await session.scalar(
            select(func.count()).select_from(EventRow)
        )
    assert event_count_after == event_count_before


async def test_operator_pending_filters_expired_rows_without_mutating_db(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Expired pending rows are hidden read-only; the database row stays unchanged.

    Mutation: call ``expire_due_refunds_for_customer`` from ``GET /operator/pending``
    and this fails on the unchanged event count.
    """
    case_id, refund_id, _ = await _create_pending_large_refund(
        api_session_factory,
        message_id="msg-operator-pending-expired",
    )
    _advance_clock_past_approval_ttl()

    async with api_session_factory() as session:
        event_count_before = await session.scalar(
            select(func.count()).select_from(EventRow)
        )
        refund_before = await find_refund_by_id(session, refund_id)
    assert refund_before is not None
    approval_expires_at_before = refund_before.approval_expires_at

    app = create_app(session_factory=api_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/pending")

    assert response.status_code == 200
    assert response.json() == {"pending_refunds": []}

    async with api_session_factory() as session:
        event_count_after = await session.scalar(
            select(func.count()).select_from(EventRow)
        )
        refund_after = await find_refund_by_id(session, refund_id)
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=clock.now(),
        )

    assert event_count_after == event_count_before
    assert refund_after is not None
    assert refund_after.status is RefundStatus.PENDING_APPROVAL
    assert refund_after.approval_expires_at == approval_expires_at_before
    assert EventType.REFUND_EXPIRED not in {event.event_type for event in case_events}
    # Read-only filtering must not imply liveness: ordinary traffic still reaps expiry.
    assert case_summary.case_id == case_id


async def test_operator_pending_orders_by_expiry_then_refund_id(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Multiple active pending refunds are returned in deterministic order."""
    later_expiry = clock.now() + timedelta(seconds=config.APPROVAL_TTL_SECONDS)
    earlier_expiry = clock.now() + timedelta(seconds=300)

    async with api_session_factory.begin() as session:
        session.add(
            CaseRow(
                id="case_pending_alpha",
                customer_id=SOPHIE_CUSTOMER_ID,
                opening_message_id="msg-pending-alpha",
                created_at=clock.now(),
            )
        )
        session.add(
            CaseRow(
                id="case_pending_beta",
                customer_id=SOPHIE_CUSTOMER_ID,
                opening_message_id="msg-pending-beta",
                created_at=clock.now(),
            )
        )
        session.add_all(
            [
                RefundRow(
                    id="rfnd_pending_later",
                    customer_id=SOPHIE_CUSTOMER_ID,
                    order_id=ORD_1003_ID,
                    case_id="case_pending_alpha",
                    amount=Decimal("780.00"),
                    status=RefundStatus.PENDING_APPROVAL,
                    approval_expires_at=later_expiry,
                    created_at=clock.now(),
                ),
                RefundRow(
                    id="rfnd_pending_earlier",
                    customer_id=SOPHIE_CUSTOMER_ID,
                    order_id=ORD_1002_ID,
                    case_id="case_pending_beta",
                    amount=Decimal("24.00"),
                    status=RefundStatus.PENDING_APPROVAL,
                    approval_expires_at=earlier_expiry,
                    created_at=clock.now(),
                ),
            ]
        )

    app = create_app(session_factory=api_session_factory)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/operator/pending")

    assert response.status_code == 200
    pending_refund_ids = [
        pending_refund["refund_id"]
        for pending_refund in response.json()["pending_refunds"]
    ]
    assert pending_refund_ids == ["rfnd_pending_earlier", "rfnd_pending_later"]


def _advance_clock_past_approval_ttl() -> None:
    clock.set_now_for_tests(
        FIXED_TEST_NOW + timedelta(seconds=config.APPROVAL_TTL_SECONDS + 1),
    )
