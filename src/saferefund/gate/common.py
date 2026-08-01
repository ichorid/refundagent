"""Shared rule-context loading and denial recording for gate operations."""

from datetime import datetime
from typing import TYPE_CHECKING, Never, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from saferefund import clock, config
from saferefund.actions.models import (
    Action,
)
from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    EscalationOrigin,
    RefundStatus,
)
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    ActionDeniedPayload,
    CaseClosedPayload,
    EscalatedPayload,
    RefundExpiredPayload,
)
from saferefund.domain.tables import CaseRow, CustomerRow, RefundRow
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import Deny
from saferefund.projections.case import project_case_summary
from saferefund.projections.customer import project_customer_summary
from saferefund.projections.order import OrderSummary, project_order_summary
from saferefund.projections.types import CustomerSeed, FoldableEvent, OrderSeed
from saferefund.repositories.cases import list_cases_for_customer_ordered
from saferefund.repositories.customers import lock_customer_for_update
from saferefund.repositories.events import append_canonical_event, load_customer_events
from saferefund.repositories.orders import find_order_by_id, list_orders_for_customer
from saferefund.repositories.refund_transitions import transition_refund_status
from saferefund.repositories.refunds import due_for_expiry_clause

if TYPE_CHECKING:
    from collections.abc import Sequence


class CaseNotFoundError(LookupError):
    """Raised when a case identifier does not match a correlation row."""

    def __init__(self, case_id: str) -> None:
        """Record the missing case identifier."""
        super().__init__(f"Case not found: {case_id}")


def session_factory_for(session: AsyncSession) -> async_sessionmaker[AsyncSession]:
    """Return a session factory sharing the async bind of an existing session."""
    bind = cast("AsyncEngine", session.bind)
    return async_sessionmaker(bind, expire_on_commit=False)


async def find_open_case_ids_for_customer(
    session: AsyncSession,
    customer_id: str,
    now: datetime,
) -> tuple[str, ...]:
    """Return open case ids for one customer in ascending creation order."""
    customer_events = await load_customer_events(session, customer_id)
    customer_event_stream: Sequence[FoldableEvent] = cast(
        "Sequence[FoldableEvent]",
        customer_events,
    )
    case_rows = await list_cases_for_customer_ordered(session, customer_id)
    open_case_ids: list[str] = []
    for case_row in case_rows:
        case_summary = project_case_summary(
            case_id=case_row.id,
            customer_id=customer_id,
            events=customer_event_stream,
            now=now,
        )
        if case_summary.status is CaseStatus.OPEN:
            open_case_ids.append(case_row.id)
    return tuple(open_case_ids)


def escalated_actor_for_origin(origin: EscalationOrigin) -> Actor:
    """Return the accountable actor for an escalation event."""
    if origin is EscalationOrigin.AGENT:
        return Actor.AGENT
    return Actor.SYSTEM


def case_outcome_for_escalation_origin(origin: EscalationOrigin) -> CaseOutcome:
    """Map escalation origin to the terminal case outcome recorded on closure."""
    match origin:
        case EscalationOrigin.AGENT | EscalationOrigin.POLICY:
            return CaseOutcome.ESCALATED
        case EscalationOrigin.STEP_LIMIT:
            return CaseOutcome.STEP_LIMIT
        case EscalationOrigin.PARSE_LIMIT:
            return CaseOutcome.PARSE_LIMIT
        case EscalationOrigin.MODEL_FAILURE:
            return CaseOutcome.MODEL_FAILURE


async def load_rule_context_for_case(
    session: AsyncSession,
    case_id: str,
) -> tuple[CaseRow, RuleContext]:
    """Load seed rows and fold projections into a policy evaluation context."""
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

    linked_order_summary: OrderSummary | None = None
    if case_summary.linked_order_id is not None:
        linked_order_row = await find_order_by_id(
            session,
            case_summary.linked_order_id,
        )
        if linked_order_row is not None:
            linked_order_summary = project_order_summary(
                OrderSeed(
                    order_id=linked_order_row.id,
                    customer_id=linked_order_row.customer_id,
                    total=linked_order_row.total,
                ),
                customer_event_stream,
                now,
            )

    customer_order_rows = await list_orders_for_customer(session, case_row.customer_id)
    customer_order_ids = frozenset(order_row.id for order_row in customer_order_rows)

    rule_context = RuleContext(
        now=now,
        customer=customer_summary,
        case=case_summary,
        linked_order=linked_order_summary,
        customer_order_ids=customer_order_ids,
        refund_approval_threshold=config.REFUND_APPROVAL_THRESHOLD,
        denial_loop_threshold=config.DENIAL_LOOP_THRESHOLD,
    )
    return case_row, rule_context


async def expire_due_refunds_for_customer(
    session: AsyncSession,
    *,
    customer_id: str,
) -> tuple[str, ...]:
    """Expire every due pending refund for one customer; return reopened case ids."""
    await lock_customer_for_update(session, customer_id)

    now = clock.now()
    statement = (
        select(RefundRow)
        .join(CaseRow, RefundRow.case_id == CaseRow.id)
        .where(
            CaseRow.customer_id == customer_id,
            RefundRow.status == RefundStatus.PENDING_APPROVAL,
            due_for_expiry_clause(now),
        )
        .order_by(RefundRow.case_id, RefundRow.id)
    )
    due_refund_rows = (await session.scalars(statement)).all()

    expired_case_ids: dict[str, None] = {}
    for refund_row in due_refund_rows:
        expired_case_id = refund_row.case_id
        if not await transition_refund_status(
            session,
            refund_row.id,
            from_status=RefundStatus.PENDING_APPROVAL,
            to_status=RefundStatus.EXPIRED,
        ):
            continue
        await append_canonical_event(
            session,
            event_type=EventType.REFUND_EXPIRED,
            customer_id=customer_id,
            case_id=expired_case_id,
            order_id=refund_row.order_id,
            actor=Actor.SYSTEM,
            channel=Channel.INTERNAL,
            payload=RefundExpiredPayload(refund_id=refund_row.id),
        )
        expired_case_ids[expired_case_id] = None

    if not expired_case_ids:
        return ()

    customer_events = await load_customer_events(session, customer_id)
    customer_event_stream: Sequence[FoldableEvent] = cast(
        "Sequence[FoldableEvent]",
        customer_events,
    )
    reopened_case_ids: list[str] = []
    for expired_case_id in sorted(expired_case_ids):
        case_summary = project_case_summary(
            case_id=expired_case_id,
            customer_id=customer_id,
            events=customer_event_stream,
            now=now,
        )
        if case_summary.status is CaseStatus.OPEN:
            reopened_case_ids.append(expired_case_id)
    return tuple(reopened_case_ids)


def action_discriminator(action: Action) -> str:
    """Return the stable action name stored on denial events."""
    return action.action


def raise_unreachable_require_approval(action: Action) -> Never:
    """RequireApproval applies only to refund proposals."""
    action_name = action_discriminator(action)
    message = f"RequireApproval is unreachable for non-refund action {action_name}"
    raise AssertionError(message)


async def append_action_denied(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    action: Action,
    deny_verdict: Deny,
) -> None:
    """Record a policy denial without calling side-effect adapters."""
    await append_canonical_event(
        session,
        event_type=EventType.ACTION_DENIED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=ActionDeniedPayload(
            action=action_discriminator(action),
            rule=deny_verdict.rule,
            agent_reason=deny_verdict.agent_reason,
            customer_reason=deny_verdict.customer_reason,
        ),
    )


async def append_escalated_and_case_closed(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    origin: EscalationOrigin,
    reason: str,
    ticket_id: str,
) -> None:
    """Append escalated and case_closed in the current transaction."""
    await append_canonical_event(
        session,
        event_type=EventType.ESCALATED,
        customer_id=customer_id,
        case_id=case_id,
        actor=escalated_actor_for_origin(origin),
        channel=Channel.INTERNAL,
        payload=EscalatedPayload(
            reason=reason,
            origin=origin,
            ticket_id=ticket_id,
        ),
    )
    await append_canonical_event(
        session,
        event_type=EventType.CASE_CLOSED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=CaseClosedPayload(
            outcome=case_outcome_for_escalation_origin(origin),
            summary=None,
        ),
    )


__all__ = [
    "CaseNotFoundError",
    "action_discriminator",
    "append_action_denied",
    "append_escalated_and_case_closed",
    "case_outcome_for_escalation_origin",
    "escalated_actor_for_origin",
    "expire_due_refunds_for_customer",
    "find_open_case_ids_for_customer",
    "load_rule_context_for_case",
    "raise_unreachable_require_approval",
    "session_factory_for",
]
