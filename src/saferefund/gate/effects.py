"""Gate side-effect helpers (rung 5): underscored names and Authorisation required."""

from datetime import datetime, timedelta
from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession

from saferefund import config, ids
from saferefund.actions.models import (
    Action,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.adapters import mailer
from saferefund.domain.enums import Actor, CaseOutcome, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CaseClosedPayload,
    OrderLinkedPayload,
    OrdersListedPayload,
    ReplySentPayload,
    VerificationRequestedPayload,
)
from saferefund.domain.tables import CaseRow, CustomerRow
from saferefund.gate.common import raise_unreachable_require_approval
from saferefund.policy.authorisation import Authorisation
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import RequireApproval, Verdict
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.orders import list_orders_for_customer


async def _allow_get_orders(
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    customer_id: str,
    case_id: str,
    action: GetOrders,
) -> None:
    """Expose the customer's order identifiers and append orders_listed."""
    authorisation.spend(case_id=case_id, action=action)
    customer_order_rows = await list_orders_for_customer(session, customer_id)
    order_ids = [order_row.id for order_row in customer_order_rows]
    await append_canonical_event(
        session,
        event_type=EventType.ORDERS_LISTED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.AGENT,
        channel=Channel.INTERNAL,
        payload=OrdersListedPayload(order_ids=order_ids),
    )


async def _allow_link_order(
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    customer_id: str,
    case_id: str,
    action: LinkOrder,
) -> None:
    """Record validated order ownership on the active case."""
    authorisation.spend(case_id=case_id, action=action)
    await append_canonical_event(
        session,
        event_type=EventType.ORDER_LINKED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=action.order_id,
        actor=Actor.AGENT,
        channel=Channel.INTERNAL,
        payload=OrderLinkedPayload(order_id=action.order_id),
    )


async def _allow_send_reply(
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    case_row: CaseRow,
    case_id: str,
    action: SendReply,
) -> None:
    """Deliver a reply to the case customer, then append reply_sent."""
    authorisation.spend(case_id=case_id, action=action)
    customer_row = await session.get(CustomerRow, case_row.customer_id)
    if customer_row is None:
        message = f"Customer not found: {case_row.customer_id}"
        raise LookupError(message)

    # Effect-first by design (§9.4): delivery may succeed without a durable event.
    mailer.send(to=customer_row.email, subject=action.subject, body=action.body)

    await append_canonical_event(
        session,
        event_type=EventType.REPLY_SENT,
        customer_id=case_row.customer_id,
        case_id=case_id,
        actor=Actor.AGENT,
        channel=Channel.INTERNAL,
        payload=ReplySentPayload(subject=action.subject, body=action.body),
    )


async def _allow_request_verification(  # noqa: PLR0913
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    case_row: CaseRow,
    case_id: str,
    action: RequestVerification,
    now: datetime,
) -> None:
    """Generate a token, send the fixed verification template, then record it."""
    authorisation.spend(case_id=case_id, action=action)
    customer_row = await session.get(CustomerRow, case_row.customer_id)
    if customer_row is None:
        message = f"Customer not found: {case_row.customer_id}"
        raise LookupError(message)

    token = ids.verification_token()
    expires_at = now + timedelta(seconds=config.VERIFICATION_TTL_SECONDS)
    verification_body = config.VERIFICATION_BODY.format(token=token)

    # Effect-first by design (§9.4): the customer may receive a token with no event.
    mailer.send(
        to=customer_row.email,
        subject=config.VERIFICATION_SUBJECT,
        body=verification_body,
    )

    await append_canonical_event(
        session,
        event_type=EventType.VERIFICATION_REQUESTED,
        customer_id=case_row.customer_id,
        case_id=case_id,
        actor=Actor.AGENT,
        channel=Channel.INTERNAL,
        payload=VerificationRequestedPayload(token=token, expires_at=expires_at),
    )


async def _allow_finish(
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    customer_id: str,
    case_id: str,
    action: Finish,
) -> None:
    """Close the case with a finished outcome and model-authored summary."""
    authorisation.spend(case_id=case_id, action=action)
    await append_canonical_event(
        session,
        event_type=EventType.CASE_CLOSED,
        customer_id=customer_id,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=CaseClosedPayload(outcome=CaseOutcome.FINISHED, summary=action.summary),
    )


async def _dispatch_require_approval_verdict(
    session: AsyncSession,
    *,
    case_row: CaseRow,
    case_id: str,
    action: Action,
    require_approval_verdict: RequireApproval,
) -> Verdict:
    """Route RequireApproval; only refund proposals are valid in production."""
    _ = session, case_row, case_id, require_approval_verdict
    match action:
        case ProposeRefund():
            message = (
                "ProposeRefund RequireApproval must be dispatched by "
                "execute_agent_action."
            )
            raise RuntimeError(message)
        case (
            GetOrders()
            | LinkOrder()
            | SendReply()
            | RequestVerification()
            | Escalate()
            | Finish()
        ):
            return raise_unreachable_require_approval(action)
        case _ as unreachable_action:
            assert_never(unreachable_action)


async def _dispatch_allow_verdict(  # noqa: PLR0913
    session: AsyncSession,
    *,
    authorisation: Authorisation,
    case_row: CaseRow,
    case_id: str,
    action: Action,
    rule_context: RuleContext,
) -> None:
    """Perform allowed side effects and append success events."""
    match action:
        case ProposeRefund():
            message = "ProposeRefund must be dispatched by execute_agent_action."
            raise RuntimeError(message)
        case GetOrders() as get_orders_action:
            await _allow_get_orders(
                session,
                authorisation=authorisation,
                customer_id=case_row.customer_id,
                case_id=case_id,
                action=get_orders_action,
            )
        case LinkOrder() as link_order_action:
            await _allow_link_order(
                session,
                authorisation=authorisation,
                customer_id=case_row.customer_id,
                case_id=case_id,
                action=link_order_action,
            )
        case SendReply() as send_reply_action:
            await _allow_send_reply(
                session,
                authorisation=authorisation,
                case_row=case_row,
                case_id=case_id,
                action=send_reply_action,
            )
        case RequestVerification() as request_verification_action:
            await _allow_request_verification(
                session,
                authorisation=authorisation,
                case_row=case_row,
                case_id=case_id,
                action=request_verification_action,
                now=rule_context.now,
            )
        case Finish() as finish_action:
            await _allow_finish(
                session,
                authorisation=authorisation,
                customer_id=case_row.customer_id,
                case_id=case_id,
                action=finish_action,
            )
        case Escalate():
            message = "Escalate must be dispatched by execute_agent_action."
            raise RuntimeError(message)
        case _ as unreachable_action:
            assert_never(unreachable_action)


__all__: list[str] = []
