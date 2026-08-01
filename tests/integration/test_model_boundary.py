"""Integration tests for complete model gateway result validation."""

from __future__ import annotations

import hashlib
from collections import deque
from typing import TYPE_CHECKING, Any

import pytest
from httpx import ASGITransport, AsyncClient

from saferefund import config
from saferefund.adapters import reset_adapters_for_tests
from saferefund.agent.gateway import ModelGateway
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.domain.enums import (
    CaseOutcome,
    CaseStatus,
    EscalationOrigin,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import InvalidOutputPayload
from saferefund.main import create_app
from saferefund.projections.case import project_case_summary
from saferefund.repositories.cases import find_case_for_customer_by_opening_message_id
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.seed import TOM_CUSTOMER_ID, TOM_EMAIL
from tests.conftest import FIXED_TEST_NOW
from tests.support.model_gateway import heuristic_gateway
from tests.support.sequence_assertions import (
    MODEL_FAILURE_REASON_PREFIX,
    assert_terminal_escalation_closure,
    model_failure_termination_sequence,
    parse_limit_termination_sequence,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker


@pytest.fixture(autouse=True)
def _reset_dependencies() -> None:
    reset_adapters_for_tests()
    reset_case_locks_for_tests()


def _assert_parse_limit_termination(case_events: list[Any]) -> None:
    assert_terminal_escalation_closure(
        case_events,
        expected_types=parse_limit_termination_sequence(),
        origin=EscalationOrigin.PARSE_LIMIT,
        outcome=CaseOutcome.PARSE_LIMIT,
    )


async def test_non_string_model_output_escalates_and_closes_case(
    api_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every malformed model-boundary result must terminate through model_failure."""
    message_id = "review-non-string-model-output"

    async def broken_propose(_self: ModelGateway, prompt: object) -> object:
        del prompt
        return None

    monkeypatch.setattr(ModelGateway, "propose", broken_propose)
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=heuristic_gateway(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": message_id,
                "subject": "Refund",
                "body": "Please help.",
            },
        )

    assert response.status_code == 200
    async with api_session_factory() as session:
        case_row = await find_case_for_customer_by_opening_message_id(
            session,
            TOM_CUSTOMER_ID,
            message_id,
        )
        assert case_row is not None
        case_events = await load_case_events(session, case_row.id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
    summary = project_case_summary(
        case_id=case_row.id,
        customer_id=TOM_CUSTOMER_ID,
        events=customer_events,
        now=FIXED_TEST_NOW,
    )
    assert summary.status is CaseStatus.CLOSED
    assert_terminal_escalation_closure(
        case_events,
        expected_types=model_failure_termination_sequence(),
        origin=EscalationOrigin.MODEL_FAILURE,
        outcome=CaseOutcome.MODEL_FAILURE,
        reason_prefix=MODEL_FAILURE_REASON_PREFIX,
    )


async def test_parser_exception_escalates_once_and_closes_case(
    api_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unexpected parser exception must close the case through model_failure."""
    message_id = "review-parser-exception"

    def exploding_parse(_raw_model_output: str) -> object:
        message = "parser exploded"
        raise RuntimeError(message)

    monkeypatch.setattr(
        "saferefund.agent.model_boundary.parse",
        exploding_parse,
    )
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=heuristic_gateway(),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": message_id,
                "subject": "Refund",
                "body": "Please help.",
            },
        )

    assert response.status_code == 200
    async with api_session_factory() as session:
        case_row = await find_case_for_customer_by_opening_message_id(
            session,
            TOM_CUSTOMER_ID,
            message_id,
        )
        assert case_row is not None
        case_events = await load_case_events(session, case_row.id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
    summary = project_case_summary(
        case_id=case_row.id,
        customer_id=TOM_CUSTOMER_ID,
        events=customer_events,
        now=FIXED_TEST_NOW,
    )
    assert summary.status is CaseStatus.CLOSED
    assert_terminal_escalation_closure(
        case_events,
        expected_types=model_failure_termination_sequence(),
        origin=EscalationOrigin.MODEL_FAILURE,
        outcome=CaseOutcome.MODEL_FAILURE,
        reason_prefix=MODEL_FAILURE_REASON_PREFIX,
        reason_suffix="RuntimeError.",
    )


def _malformed_response_at_byte_limit() -> bytes:
    """Return malformed ASCII JSON padded to exactly the configured gateway byte cap."""
    prefix = b"not-valid-json:"
    padding_len = config.MODEL_RESPONSE_MAX_BYTES - len(prefix)
    return prefix + (b"x" * padding_len)


class _ScriptedResponseTransport:
    """Return scripted raw gateway responses in invocation order."""

    def __init__(self, responses: list[bytes]) -> None:
        self._pending = deque(responses)

    async def invoke(self, request_bytes: bytes) -> bytes:
        del request_bytes
        if not self._pending:
            message = "scripted model responses exhausted"
            raise RuntimeError(message)
        return self._pending.popleft()


class _OversizedResponseTransport:
    """Return one raw response that exceeds the configured byte cap."""

    async def invoke(self, request_bytes: bytes) -> bytes:
        del request_bytes
        return b"x" * (config.MODEL_RESPONSE_MAX_BYTES + 1)


async def test_oversized_model_response_is_not_persisted_in_full(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """At-limit malformed bytes store bounded audit metadata and hit parse limit."""
    message_id = "review-at-limit-model-response"
    at_limit = _malformed_response_at_byte_limit()
    assert len(at_limit) == config.MODEL_RESPONSE_MAX_BYTES
    gateway = ModelGateway(
        _ScriptedResponseTransport([at_limit] * config.MAX_INVALID_OUTPUTS)
    )
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=gateway,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": message_id,
                "subject": "Refund",
                "body": "Please help.",
            },
        )

    assert response.status_code == 200
    async with api_session_factory() as session:
        case_row = await find_case_for_customer_by_opening_message_id(
            session,
            TOM_CUSTOMER_ID,
            message_id,
        )
        assert case_row is not None
        case_events = await load_case_events(session, case_row.id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
    summary = project_case_summary(
        case_id=case_row.id,
        customer_id=TOM_CUSTOMER_ID,
        events=customer_events,
        now=FIXED_TEST_NOW,
    )
    assert summary.status is CaseStatus.CLOSED
    invalid_events = [
        event for event in case_events if event.event_type is EventType.INVALID_OUTPUT
    ]
    assert [event.event_type for event in case_events] == list(
        parse_limit_termination_sequence()
    )
    at_limit_text = at_limit.decode("utf-8")
    expected_digest = hashlib.sha256(at_limit).hexdigest()
    for event in invalid_events:
        payload = InvalidOutputPayload.model_validate(event.payload)
        assert (
            len(payload.preview.encode("utf-8"))
            <= config.INVALID_OUTPUT_PREVIEW_MAX_BYTES
        )
        assert payload.byte_count == config.MODEL_RESPONSE_MAX_BYTES
        assert payload.sha256 == expected_digest
        assert payload.preview != at_limit_text
        payload.preview.encode("utf-8")
    _assert_parse_limit_termination(case_events)


async def test_raw_gateway_response_over_byte_cap_closes_without_audit_persistence(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Over-cap gateway bytes close via model_failure without an audit row."""
    message_id = "review-oversized-model-response"
    gateway = ModelGateway(_OversizedResponseTransport())
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=gateway,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": message_id,
                "subject": "Refund",
                "body": "Please help.",
            },
        )

    assert response.status_code == 200
    async with api_session_factory() as session:
        case_row = await find_case_for_customer_by_opening_message_id(
            session,
            TOM_CUSTOMER_ID,
            message_id,
        )
        assert case_row is not None
        case_events = await load_case_events(session, case_row.id)
        customer_events = await load_customer_events(session, TOM_CUSTOMER_ID)
    summary = project_case_summary(
        case_id=case_row.id,
        customer_id=TOM_CUSTOMER_ID,
        events=customer_events,
        now=FIXED_TEST_NOW,
    )
    assert summary.status is CaseStatus.CLOSED
    invalid_events = [
        event for event in case_events if event.event_type is EventType.INVALID_OUTPUT
    ]
    assert invalid_events == []
    assert_terminal_escalation_closure(
        case_events,
        expected_types=model_failure_termination_sequence(),
        origin=EscalationOrigin.MODEL_FAILURE,
        outcome=CaseOutcome.MODEL_FAILURE,
        reason_prefix=MODEL_FAILURE_REASON_PREFIX,
    )
