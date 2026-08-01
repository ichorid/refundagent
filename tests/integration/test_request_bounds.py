"""HTTP request field length bounds reject oversize input before side effects."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from sqlalchemy import func, select

from saferefund import config
from saferefund.adapters import mailer, payment, reset_adapters_for_tests, ticketing
from saferefund.domain.tables import CaseRow, EventRow
from saferefund.repositories.seed import (
    SOPHIE_EMAIL,
    UNKNOWN_SENDER_EMAIL,
)

if TYPE_CHECKING:
    from httpx import AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

_INBOUND_BASE = {
    "envelope_from": SOPHIE_EMAIL,
    "message_id": "msg-bounds-base",
    "subject": "Refund help",
    "body": "Please assist with my order.",
}

_OPERATOR_APPROVE_BASE = {
    "refund_id": "rfnd_placeholder",
    "operator_id": "op_bounds",
}

_OPERATOR_REJECT_BASE = {
    "refund_id": "rfnd_placeholder",
    "operator_id": "op_bounds",
    "reason": "Not eligible",
}

_VERIFICATION_BASE = {"token": "tok_bounds"}


def _oversized(value: str, limit: int) -> str:
    return value + ("x" * (limit + 1))


@pytest.fixture(autouse=True)
def _reset_adapter_state() -> None:
    reset_adapters_for_tests()


async def _assert_no_new_writes(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    baseline_cases: int,
    baseline_events: int,
) -> None:
    async with session_factory() as session:
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        event_count = await session.scalar(select(func.count()).select_from(EventRow))
    assert case_count == baseline_cases
    assert event_count == baseline_events
    assert mailer.outbox == []
    assert payment.calls == []
    assert ticketing.escalations == []


@pytest.mark.parametrize(
    ("field_name", "limit_attr", "base_payload"),
    [
        ("envelope_from", "HTTP_ENVELOPE_FROM_MAX_LENGTH", _INBOUND_BASE),
        ("message_id", "HTTP_MESSAGE_ID_MAX_LENGTH", _INBOUND_BASE),
        ("subject", "HTTP_INBOUND_SUBJECT_MAX_LENGTH", _INBOUND_BASE),
        ("body", "HTTP_INBOUND_BODY_MAX_LENGTH", _INBOUND_BASE),
        ("operator_id", "HTTP_OPERATOR_ID_MAX_LENGTH", _OPERATOR_APPROVE_BASE),
        ("reason", "HTTP_OPERATOR_REASON_MAX_LENGTH", _OPERATOR_REJECT_BASE),
        ("token", "HTTP_VERIFICATION_TOKEN_MAX_LENGTH", _VERIFICATION_BASE),
    ],
)
async def test_oversized_request_field_is_rejected_without_writes(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
    field_name: str,
    limit_attr: str,
    base_payload: dict[str, str],
) -> None:
    """Oversized HTTP strings return 422 with zero persistence or adapter calls."""
    limit = getattr(config, limit_attr)
    payload = dict(base_payload)
    payload[field_name] = _oversized(payload[field_name], limit)
    baseline_cases, baseline_events = await _baseline_counts(api_session_factory)

    if field_name in _INBOUND_BASE:
        response = await api_client.post("/inbound-email", json=payload)
    elif field_name == "operator_id":
        response = await api_client.post("/operator/approve", json=payload)
    elif field_name == "reason":
        response = await api_client.post("/operator/reject", json=payload)
    else:
        response = await api_client.post("/verification/confirm", json=payload)

    assert response.status_code == 422
    await _assert_no_new_writes(
        api_session_factory,
        baseline_cases=baseline_cases,
        baseline_events=baseline_events,
    )


async def _baseline_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int]:
    async with session_factory() as session:
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        event_count = await session.scalar(select(func.count()).select_from(EventRow))
    return case_count or 0, event_count or 0


async def test_oversized_unknown_sender_envelope_is_rejected_without_mail(
    api_client: AsyncClient,
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Unknown-sender routing must not run when envelope_from fails validation."""
    baseline_cases, baseline_events = await _baseline_counts(api_session_factory)
    response = await api_client.post(
        "/inbound-email",
        json={
            **_INBOUND_BASE,
            "envelope_from": UNKNOWN_SENDER_EMAIL
            + ("z" * (config.HTTP_ENVELOPE_FROM_MAX_LENGTH + 1)),
        },
    )
    assert response.status_code == 422
    await _assert_no_new_writes(
        api_session_factory,
        baseline_cases=baseline_cases,
        baseline_events=baseline_events,
    )
