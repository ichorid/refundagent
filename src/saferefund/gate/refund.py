"""Refund proposal transaction and post-payment execution per architecture §14.1."""

from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import assert_never, cast

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import clock, config, ids
from saferefund.actions.models import ProposeRefund
from saferefund.adapters import payment
from saferefund.domain.enums import Actor, Channel, RefundStatus
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    RefundApprovalRequiredPayload,
    RefundApprovedPayload,
    RefundAutoApprovedPayload,
    RefundExecutedPayload,
    RefundProposedPayload,
    RefundRejectedPayload,
)
from saferefund.domain.tables import CaseRow, RefundRow
from saferefund.gate.common import (
    append_action_denied,
    load_rule_context_for_case,
)
from saferefund.policy.authorisation import Authorisation
from saferefund.policy.policy import authorise
from saferefund.policy.verdicts import (
    Allow,
    Deny,
    ForceEscalate,
    RequireApproval,
    Verdict,
)
from saferefund.repositories.customers import (
    hold_customer_advisory_lock,
    lock_customer_for_update,
)
from saferefund.repositories.events import append_canonical_event
from saferefund.repositories.refund_transitions import (
    RefundIntentBinding,
    transition_refund_status,
)
from saferefund.repositories.refunds import find_open_refund_for_order
from saferefund.repositories.relational_scope import validate_refund_relational_scope

_MONEY_QUANTUM = Decimal("0.01")


def quantize_refund_amount(amount: Decimal) -> Decimal:
    """Round a validated refund amount to two decimal places before persistence."""
    return amount.quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _open_refund_race_deny() -> Deny:
    """Return the canonical R_OPEN_REFUND denial used after a unique-index race."""
    return Deny(
        rule="R_OPEN_REFUND",
        agent_reason="This order already has an open refund request.",
        customer_reason="There is already a refund in progress for this order.",
    )


async def _append_refund_proposed(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    amount: Decimal,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_PROPOSED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.AGENT,
        channel=Channel.INTERNAL,
        payload=RefundProposedPayload(refund_id=refund_id, amount=amount),
    )


async def _append_refund_auto_approved(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_AUTO_APPROVED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=RefundAutoApprovedPayload(refund_id=refund_id),
    )


async def append_refund_approved(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    operator_id: str,
) -> None:
    """Append refund_approved after operator approval succeeds."""
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_APPROVED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.OPERATOR,
        channel=Channel.OPERATOR_API,
        payload=RefundApprovedPayload(refund_id=refund_id, operator_id=operator_id),
    )


async def append_refund_rejected(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    operator_id: str,
    reason: str,
) -> None:
    """Append refund_rejected after operator rejection succeeds."""
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_REJECTED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.OPERATOR,
        channel=Channel.OPERATOR_API,
        payload=RefundRejectedPayload(
            refund_id=refund_id,
            operator_id=operator_id,
            reason=reason,
        ),
    )


async def _append_refund_approval_required(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    amount: Decimal,
    rule: str,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_APPROVAL_REQUIRED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=RefundApprovalRequiredPayload(
            refund_id=refund_id,
            amount=amount,
            rule=rule,
        ),
    )


async def _append_refund_executed(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    amount: Decimal,
    provider_ref: str,
) -> None:
    await append_canonical_event(
        session,
        event_type=EventType.REFUND_EXECUTED,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=RefundExecutedPayload(
            refund_id=refund_id,
            amount=amount,
            provider_ref=provider_ref,
        ),
    )


async def _record_refund_executed_after_payment(  # noqa: PLR0913
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    amount: Decimal,
    provider_ref: str,
    acquire_customer_lock: bool = True,
) -> None:
    """Mark a refund executed after payment in a customer-locked transaction."""
    if acquire_customer_lock:
        await lock_customer_for_update(session, customer_id)
    refund_row = await session.get(RefundRow, refund_id)
    if refund_row is None:
        message = f"Refund not found: {refund_id}"
        raise LookupError(message)
    if not await transition_refund_status(
        session,
        refund_id,
        from_status=RefundStatus.APPROVED,
        to_status=RefundStatus.EXECUTED,
        intent_binding=RefundIntentBinding(
            order_id=order_id,
            case_id=case_id,
            amount=amount,
        ),
    ):
        message = f"failed to mark refund {refund_id} executed after committed payment"
        raise RuntimeError(message)
    await _append_refund_executed(
        session,
        customer_id=customer_id,
        case_id=case_id,
        order_id=order_id,
        refund_id=refund_id,
        amount=amount,
        provider_ref=provider_ref,
    )


async def _persist_refund_proposal(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    action: ProposeRefund,
) -> tuple[Verdict, bool, str, Decimal, str]:
    """Evaluate policy under lock, insert the row, and append proposal events."""
    await lock_customer_for_update(session, customer_id)
    _, rule_context = await load_rule_context_for_case(session, case_id)
    verdict_or_auth = authorise(rule_context, action)

    if isinstance(verdict_or_auth, Deny):
        await append_action_denied(
            session,
            customer_id=customer_id,
            case_id=case_id,
            action=action,
            deny_verdict=verdict_or_auth,
        )
        return verdict_or_auth, False, "", Decimal(0), ""

    if isinstance(verdict_or_auth, ForceEscalate):
        return verdict_or_auth, False, "", Decimal(0), ""

    linked_order_id = rule_context.case.linked_order_id
    if linked_order_id is None:
        message = "Refund proposal requires a linked order."
        raise RuntimeError(message)

    auto_approved = False
    approval_rule = ""
    if isinstance(verdict_or_auth, Authorisation):
        verdict_or_auth.spend(case_id=case_id, action=action)
        auto_approved = True
    elif isinstance(verdict_or_auth, RequireApproval):
        approval_rule = verdict_or_auth.rule
    else:
        assert_never(verdict_or_auth)

    quantized_amount = quantize_refund_amount(action.amount)
    refund_id_value = ids.refund_id()
    now = clock.now()
    approval_expires_at = now + timedelta(seconds=config.APPROVAL_TTL_SECONDS)

    refund_customer_id = await validate_refund_relational_scope(
        session,
        case_id=case_id,
        order_id=linked_order_id,
    )
    if refund_customer_id != customer_id:
        message = (
            f"refund scope mismatch: case {case_id} and order {linked_order_id} "
            f"belong to customer {refund_customer_id}, not {customer_id}"
        )
        raise RuntimeError(message)

    refund_row = RefundRow(
        id=refund_id_value,
        customer_id=refund_customer_id,
        order_id=linked_order_id,
        case_id=case_id,
        amount=quantized_amount,
        status=RefundStatus.PENDING_APPROVAL,
        approval_expires_at=approval_expires_at,
        created_at=now,
    )
    session.add(refund_row)
    await session.flush()

    await _append_refund_proposed(
        session,
        customer_id=customer_id,
        case_id=case_id,
        order_id=linked_order_id,
        refund_id=refund_id_value,
        amount=quantized_amount,
    )

    if isinstance(verdict_or_auth, Authorisation):
        if not await transition_refund_status(
            session,
            refund_id_value,
            from_status=RefundStatus.PENDING_APPROVAL,
            to_status=RefundStatus.APPROVED,
            intent_binding=RefundIntentBinding(
                order_id=linked_order_id,
                case_id=case_id,
                amount=quantized_amount,
            ),
        ):
            message = f"failed to auto-approve refund {refund_id_value} after proposal"
            raise RuntimeError(message)
        await _append_refund_auto_approved(
            session,
            customer_id=customer_id,
            case_id=case_id,
            order_id=linked_order_id,
            refund_id=refund_id_value,
        )
    elif isinstance(verdict_or_auth, RequireApproval):
        await _append_refund_approval_required(
            session,
            customer_id=customer_id,
            case_id=case_id,
            order_id=linked_order_id,
            refund_id=refund_id_value,
            amount=quantized_amount,
            rule=approval_rule,
        )

    locked_verdict: Verdict = (
        Allow() if auto_approved else cast("RequireApproval", verdict_or_auth)
    )
    return (
        locked_verdict,
        auto_approved,
        refund_id_value,
        quantized_amount,
        linked_order_id,
    )


async def _append_open_refund_race_denial(
    session: AsyncSession,
    *,
    customer_id: str,
    case_id: str,
    action: ProposeRefund,
) -> Deny:
    """Record R_OPEN_REFUND after rolling back a failed proposal transaction."""
    deny_verdict = _open_refund_race_deny()
    await append_action_denied(
        session,
        customer_id=customer_id,
        case_id=case_id,
        action=action,
        deny_verdict=deny_verdict,
    )
    return deny_verdict


async def transition_pending_refund_status(
    session: AsyncSession,
    refund_id: str,
    new_status: RefundStatus,
    *,
    intent_binding: RefundIntentBinding | None = None,
) -> bool:
    """Atomically move one refund from pending_approval to new_status.

    Returns True when exactly one row was updated. A zero rowcount means another
    transaction already acted on this refund and the caller must return CONFLICT.
    """
    return await transition_refund_status(
        session,
        refund_id,
        from_status=RefundStatus.PENDING_APPROVAL,
        to_status=new_status,
        intent_binding=intent_binding,
    )


async def commit_refund_intent_before_payment(session: AsyncSession) -> None:
    """Commit durable refund intent so payment never precedes a persisted record."""
    if session.in_transaction():
        await session.commit()


async def execute_payment_after_committed_intent(  # noqa: PLR0913
    session_factory: async_sessionmaker[AsyncSession],
    *,
    customer_id: str,
    case_id: str,
    order_id: str,
    refund_id: str,
    amount: Decimal,
) -> None:
    """Record payment and refund_executed after durable intent is committed."""
    payment_result = await payment.refund(
        idempotency_key=refund_id,
        amount=amount,
    )
    async with session_factory.begin() as execution_session:
        await lock_customer_for_update(execution_session, customer_id)
        await _record_refund_executed_after_payment(
            execution_session,
            customer_id=customer_id,
            case_id=case_id,
            order_id=order_id,
            refund_id=refund_id,
            amount=amount,
            provider_ref=payment_result.provider_ref,
            acquire_customer_lock=False,
        )


async def execute_propose_refund(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    case_row: CaseRow,
    case_id: str,
    action: ProposeRefund,
) -> Verdict:
    """Run the §14.1 refund transaction and optional post-commit payment."""
    customer_id = case_row.customer_id
    _, rule_context = await load_rule_context_for_case(session, case_id)
    linked_order_id = rule_context.case.linked_order_id

    async with hold_customer_advisory_lock(session_factory, customer_id):
        try:
            async with session.begin_nested():
                (
                    verdict,
                    auto_approved,
                    refund_id_value,
                    quantized_amount,
                    linked_order_id,
                ) = await _persist_refund_proposal(
                    session,
                    customer_id=customer_id,
                    case_id=case_id,
                    action=action,
                )
        except IntegrityError:
            if linked_order_id is None:
                raise
            if await find_open_refund_for_order(session, linked_order_id) is None:
                raise
            return await _append_open_refund_race_denial(
                session,
                customer_id=customer_id,
                case_id=case_id,
                action=action,
            )

        if isinstance(verdict, Deny | ForceEscalate):
            return verdict

        await commit_refund_intent_before_payment(session)

        if auto_approved:
            await execute_payment_after_committed_intent(
                session_factory,
                customer_id=customer_id,
                case_id=case_id,
                order_id=linked_order_id,
                refund_id=refund_id_value,
                amount=quantized_amount,
            )
            return Allow()

        return verdict


__all__ = [
    "append_refund_approved",
    "append_refund_rejected",
    "commit_refund_intent_before_payment",
    "execute_payment_after_committed_intent",
    "execute_propose_refund",
    "quantize_refund_amount",
    "transition_pending_refund_status",
]
