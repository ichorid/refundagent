"""Unit tests for model gateway runtime validation at the trusted boundary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pytest

from saferefund.adapters import reset_adapters_for_tests, ticketing
from saferefund.agent.gateway import ModelGateway  # noqa: TC001
from saferefund.agent.locks import reset_case_locks_for_tests
from saferefund.agent.model_boundary import invoke_model_boundary
from saferefund.agent.prompt import build_prompt
from saferefund.domain.enums import CaseOutcome, CaseStatus, EscalationOrigin
from saferefund.domain.events import EventType
from saferefund.domain.payloads import CaseClosedPayload, EscalatedPayload
from saferefund.projections.case import project_case_summary
from saferefund.projections.customer import project_customer_summary
from saferefund.projections.types import CustomerSeed
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID
from tests.conftest import FIXED_TEST_NOW
from tests.invariants.scenario import open_case_row

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.agent.prompt import Prompt

_MODEL_FAILURE_REASON_PREFIX = "The model call failed with an exception of type"


class _HostileStr(str):  # noqa: SLOT000
    """A str subclass rejected by exact-runtime-type validation."""


class _RaisingStr(str):  # noqa: SLOT000
    """A str subclass whose string conversion raises."""

    def __str__(self) -> str:
        message = "hostile str conversion"
        raise ValueError(message)


@dataclass(slots=True)
class _RuntimeValueGateway:
    """Minimal gateway stub returning one configured runtime value."""

    runtime_value: object

    async def propose(self, prompt: Prompt) -> object:
        del prompt
        return self.runtime_value


@pytest.fixture(autouse=True)
def _reset_loop_dependencies() -> None:
    reset_adapters_for_tests()
    reset_case_locks_for_tests()


async def _open_case_for_boundary(
    session: AsyncSession,
    *,
    case_id: str,
) -> None:
    await open_case_row(
        session,
        case_id=case_id,
        customer_id=SOPHIE_CUSTOMER_ID,
        opening_message_id=f"msg-{case_id}",
    )


def _minimal_prompt() -> Prompt:
    case_summary = project_case_summary(
        case_id="case_boundary_unit",
        customer_id=SOPHIE_CUSTOMER_ID,
        events=[],
        now=FIXED_TEST_NOW,
    )
    customer_summary = project_customer_summary(
        CustomerSeed(customer_id=SOPHIE_CUSTOMER_ID, email="sophie@example.com"),
        [],
        FIXED_TEST_NOW,
    )
    return build_prompt(case_summary, customer_summary, [], ())


def _assert_single_model_failure_termination(case_events: list[Any]) -> None:
    escalated_events = [
        event for event in case_events if event.event_type is EventType.ESCALATED
    ]
    closed_events = [
        event for event in case_events if event.event_type is EventType.CASE_CLOSED
    ]
    assert len(escalated_events) == 1
    assert len(closed_events) == 1
    assert case_events[-2] is escalated_events[0]
    assert case_events[-1] is closed_events[0]

    escalated_payload = EscalatedPayload.model_validate(escalated_events[0].payload)
    closed_payload = CaseClosedPayload.model_validate(closed_events[0].payload)
    assert escalated_payload.origin is EscalationOrigin.MODEL_FAILURE
    assert closed_payload.outcome is CaseOutcome.MODEL_FAILURE
    assert escalated_payload.reason.startswith(_MODEL_FAILURE_REASON_PREFIX)
    assert escalated_payload.reason.endswith(".")
    assert "{" not in escalated_payload.reason
    assert "}" not in escalated_payload.reason
    assert len(ticketing.escalations) == 1
    assert escalated_payload.ticket_id == ticketing.escalations[0].ticket_id


@pytest.mark.parametrize(
    "runtime_value",
    [
        None,
        b"not-a-string",
        [],
        {},
        _HostileStr('{"action": "finish", "summary": "hostile"}'),
        _RaisingStr("hostile"),
    ],
    ids=[
        "none",
        "bytes",
        "list",
        "dict",
        "hostile_str_subclass",
        "raising_str_subclass",
    ],
)
async def test_protocol_violations_return_model_failure(
    seeded_session_factory: async_sessionmaker[AsyncSession],
    runtime_value: object,
) -> None:
    """Every non-exact-str gateway result closes the case through model_failure."""
    case_id = f"case_protocol_{type(runtime_value).__name__}"

    async with seeded_session_factory.begin() as session:
        await _open_case_for_boundary(session, case_id=case_id)
        boundary_result = await invoke_model_boundary(
            session,
            case_id=case_id,
            model_gateway=cast("ModelGateway", _RuntimeValueGateway(runtime_value)),
            prompt=_minimal_prompt(),
        )

    assert boundary_result is None

    async with seeded_session_factory() as session:
        case_events = await load_case_events(session, case_id)
        customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
        case_summary = project_case_summary(
            case_id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            events=customer_events,
            now=FIXED_TEST_NOW,
        )

    assert case_summary.status is CaseStatus.CLOSED
    assert [event.event_type for event in case_events] == [
        EventType.CASE_OPENED,
        EventType.ESCALATED,
        EventType.CASE_CLOSED,
    ]
    _assert_single_model_failure_termination(case_events)
