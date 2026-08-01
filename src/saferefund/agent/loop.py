"""Resumable agent loop with hard step and parse limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from saferefund import clock, config
from saferefund.agent.locks import case_execution_lock
from saferefund.agent.model_boundary import invoke_model_boundary
from saferefund.agent.parsing import ParseFailure, ParseSuccess
from saferefund.agent.prompt import (
    OrderSeedView,
    build_prompt,
    disclosed_order_ids,
    prompt_envelope_violation,
)
from saferefund.bounds import bound_invalid_output_audit
from saferefund.domain.enums import Actor, CaseStatus, Channel, EscalationOrigin
from saferefund.domain.events import EventType
from saferefund.domain.payloads import InvalidOutputPayload
from saferefund.domain.tables import CaseRow, CustomerRow
from saferefund.gate.operations import (
    CaseNotFoundError,
    escalate_case,
    execute_agent_action,
    expire_due_refunds_for_customer,
)
from saferefund.projections.case import CaseSummary, project_case_summary
from saferefund.projections.customer import CustomerSummary, project_customer_summary
from saferefund.projections.types import CustomerSeed, FoldableEvent
from saferefund.repositories.events import append_canonical_event, load_customer_events
from saferefund.repositories.orders import load_disclosed_order_rows

if TYPE_CHECKING:
    from collections.abc import Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.agent.gateway import ModelGateway

_STEP_LIMIT_REASON = (
    "Agent exceeded the maximum number of steps permitted for this case."
)
_PARSE_LIMIT_REASON = (
    "Agent exceeded the maximum number of consecutive invalid outputs."
)


@dataclass(frozen=True, slots=True)
class _LoopPromptContext:
    case_row: CaseRow
    case_summary: CaseSummary
    customer_summary: CustomerSummary
    case_events: Sequence[FoldableEvent]
    order_seeds: tuple[OrderSeedView, ...]


async def append_invalid_output(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    raw_model_output: str,
    parse_error: str,
) -> None:
    """Record one unparseable model output without reaching side-effect adapters."""
    preview, byte_count, digest = bound_invalid_output_audit(raw_model_output)
    await append_canonical_event(
        session,
        event_type=EventType.INVALID_OUTPUT,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=InvalidOutputPayload(
            preview=preview,
            byte_count=byte_count,
            sha256=digest,
            error=parse_error,
        ),
    )


async def _loop_terminates_at_entry(
    session: AsyncSession,
    case_id: str,
    case_summary: CaseSummary,
) -> bool:
    """Return True when the loop must stop before another model call."""
    if case_summary.status is CaseStatus.CLOSED:
        return True
    if case_summary.status in (
        CaseStatus.AWAITING_APPROVAL,
        CaseStatus.AWAITING_VERIFICATION,
    ):
        return True
    if case_summary.step_count >= config.MAX_AGENT_STEPS:
        await escalate_case(
            session,
            case_id,
            origin=EscalationOrigin.STEP_LIMIT,
            reason=_STEP_LIMIT_REASON,
        )
        return True
    if case_summary.consecutive_invalid_outputs >= config.MAX_INVALID_OUTPUTS:
        await escalate_case(
            session,
            case_id,
            origin=EscalationOrigin.PARSE_LIMIT,
            reason=_PARSE_LIMIT_REASON,
        )
        return True
    return False


def _accumulate_peer_case_ids(
    *,
    current_case_id: str,
    reopened_case_ids: tuple[str, ...],
    accumulated_peer_ids: list[str],
    seen_peer_ids: set[str],
) -> None:
    """Record every non-current sweep return once, in first-seen order."""
    for peer_case_id in reopened_case_ids:
        if peer_case_id == current_case_id or peer_case_id in seen_peer_ids:
            continue
        accumulated_peer_ids.append(peer_case_id)
        seen_peer_ids.add(peer_case_id)


async def run_agent_loop(
    session: AsyncSession,
    case_id: str,
    model_gateway: ModelGateway,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> tuple[str, ...]:
    """Drive one case loop and return non-current ids reopened by its sweeps."""
    if type(model_gateway) is not ModelGateway:
        message = "run_agent_loop requires a trusted ModelGateway instance"
        raise TypeError(message)
    accumulated_peer_ids: list[str] = []
    seen_peer_ids: set[str] = set()

    async with case_execution_lock(case_id):
        while True:
            case_row = await session.get(CaseRow, case_id)
            if case_row is None:
                raise CaseNotFoundError(case_id)
            reopened_case_ids = await expire_due_refunds_for_customer(
                session,
                customer_id=case_row.customer_id,
            )
            _accumulate_peer_case_ids(
                current_case_id=case_id,
                reopened_case_ids=reopened_case_ids,
                accumulated_peer_ids=accumulated_peer_ids,
                seen_peer_ids=seen_peer_ids,
            )
            loop_context = await _load_loop_prompt_context(session, case_id)
            case_summary = loop_context.case_summary

            if await _loop_terminates_at_entry(session, case_id, case_summary):
                return tuple(accumulated_peer_ids)

            prompt = build_prompt(
                case_summary,
                loop_context.customer_summary,
                loop_context.case_events,
                loop_context.order_seeds,
            )
            envelope_reason = prompt_envelope_violation(
                prompt,
                authorized_order_count=len(loop_context.order_seeds),
            )
            if envelope_reason is not None:
                await escalate_case(
                    session,
                    case_id,
                    origin=EscalationOrigin.MODEL_FAILURE,
                    reason=envelope_reason,
                )
                return tuple(accumulated_peer_ids)
            boundary_result = await invoke_model_boundary(
                session,
                case_id=case_id,
                model_gateway=model_gateway,
                prompt=prompt,
            )
            if boundary_result is None:
                return tuple(accumulated_peer_ids)

            match boundary_result.outcome:
                case ParseFailure(error=parse_error):
                    await append_invalid_output(
                        session,
                        customer_id=loop_context.case_row.customer_id,
                        case_id=case_id,
                        raw_model_output=boundary_result.raw_model_output,
                        parse_error=parse_error,
                    )
                    continue
                case ParseSuccess(action=action):
                    await execute_agent_action(
                        session,
                        case_id,
                        action,
                        session_factory=session_factory,
                    )
                    continue


async def _load_loop_prompt_context(
    session: AsyncSession,
    case_id: str,
) -> _LoopPromptContext:
    case_row = await session.get(CaseRow, case_id)
    if case_row is None:
        raise CaseNotFoundError(case_id)

    customer_row = await session.get(CustomerRow, case_row.customer_id)
    if customer_row is None:
        message = f"Customer not found: {case_row.customer_id}"
        raise LookupError(message)

    customer_events = await load_customer_events(session, case_row.customer_id)
    customer_event_stream: Sequence[FoldableEvent] = cast(
        "Sequence[FoldableEvent]",
        customer_events,
    )
    case_events = [event for event in customer_event_stream if event.case_id == case_id]
    now = clock.now()

    customer_summary = project_customer_summary(
        CustomerSeed(customer_id=customer_row.id, email=customer_row.email),
        customer_event_stream,
        now,
    )
    case_summary = project_case_summary(
        case_id=case_id,
        customer_id=case_row.customer_id,
        events=customer_event_stream,
        now=now,
    )
    disclosed_ids = (
        disclosed_order_ids(case_events) if case_summary.orders_listed else frozenset()
    )
    disclosed_order_rows = await load_disclosed_order_rows(
        session,
        case_row.customer_id,
        disclosed_ids,
    )
    order_seeds = tuple(
        OrderSeedView(
            order_id=order_row.id,
            customer_id=order_row.customer_id,
            item=order_row.item,
            total=order_row.total,
            status=order_row.status,
        )
        for order_row in disclosed_order_rows
    )

    return _LoopPromptContext(
        case_row=case_row,
        case_summary=case_summary,
        customer_summary=customer_summary,
        case_events=case_events,
        order_seeds=order_seeds,
    )


__all__ = [
    "append_invalid_output",
    "expire_due_refunds_for_customer",
    "run_agent_loop",
]
