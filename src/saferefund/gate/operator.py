"""Operator approve and reject gate operations."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.domain.enums import RefundStatus
from saferefund.domain.tables import CaseRow
from saferefund.gate.common import expire_due_refunds_for_customer, session_factory_for
from saferefund.gate.outcomes import OperatorOutcome, OperatorResultKind
from saferefund.gate.refund import (
    append_refund_approved,
    append_refund_rejected,
    commit_refund_intent_before_payment,
    execute_payment_after_committed_intent,
    transition_pending_refund_status,
)
from saferefund.repositories.customers import (
    hold_customer_advisory_lock,
    lock_customer_for_update,
)
from saferefund.repositories.refund_intent import (
    refund_payment_amount_from_evidence,
    validate_refund_intent_against_proposed_evidence,
)
from saferefund.repositories.refund_transitions import RefundIntentBinding
from saferefund.repositories.refunds import require_refund_by_id


async def _customer_id_for_case(session: AsyncSession, case_id: str) -> str:
    case_row = await session.get(CaseRow, case_id)
    if case_row is None:
        message = f"Case not found: {case_id}"
        raise LookupError(message)
    return case_row.customer_id


async def approve_refund(
    session: AsyncSession,
    refund_id: str,
    operator_id: str,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> OperatorOutcome:
    """Approve a pending refund, commit approval, then execute payment per §14.1."""
    refund_row = await require_refund_by_id(session, refund_id)
    case_id = refund_row.case_id
    refund_session_factory = session_factory or session_factory_for(session)
    customer_id = await _customer_id_for_case(session, case_id)

    async with hold_customer_advisory_lock(refund_session_factory, customer_id):
        await lock_customer_for_update(session, customer_id)
        # expire_due_refunds_for_customer also takes the customer lock;
        # kept deliberately so the helper stays correct without a prior lock.
        reopened_case_ids = await expire_due_refunds_for_customer(
            session,
            customer_id=customer_id,
        )

        refund_row = await require_refund_by_id(session, refund_id)
        await session.refresh(refund_row)
        if refund_row.status is not RefundStatus.PENDING_APPROVAL:
            return OperatorOutcome(
                kind=OperatorResultKind.CONFLICT,
                case_id=case_id,
                refund_id=refund_id,
                refund_status=refund_row.status,
                reopened_case_ids=reopened_case_ids,
            )

        proposed_payload = await validate_refund_intent_against_proposed_evidence(
            session,
            refund_row,
            customer_id=customer_id,
        )

        order_id = refund_row.order_id
        intent_binding = RefundIntentBinding(
            order_id=order_id,
            case_id=case_id,
            amount=proposed_payload.amount,
        )
        payment_amount = refund_payment_amount_from_evidence(proposed_payload)

        if not await transition_pending_refund_status(
            session,
            refund_id,
            RefundStatus.APPROVED,
            intent_binding=intent_binding,
        ):
            await session.refresh(refund_row)
            return OperatorOutcome(
                kind=OperatorResultKind.CONFLICT,
                case_id=case_id,
                refund_id=refund_id,
                refund_status=refund_row.status,
                reopened_case_ids=reopened_case_ids,
            )

        await append_refund_approved(
            session,
            customer_id=customer_id,
            case_id=case_id,
            order_id=order_id,
            refund_id=refund_id,
            operator_id=operator_id,
        )

        await commit_refund_intent_before_payment(session)

        await execute_payment_after_committed_intent(
            refund_session_factory,
            customer_id=customer_id,
            case_id=case_id,
            order_id=order_id,
            refund_id=refund_id,
            amount=payment_amount,
        )

        return OperatorOutcome(
            kind=OperatorResultKind.APPROVED,
            case_id=case_id,
            refund_id=refund_id,
            refund_status=RefundStatus.EXECUTED,
            reopened_case_ids=reopened_case_ids,
        )


async def reject_refund(
    session: AsyncSession,
    refund_id: str,
    operator_id: str,
    reason: str,
) -> OperatorOutcome:
    """Reject a pending refund without payment and return the owning case for resume."""
    refund_row = await require_refund_by_id(session, refund_id)
    case_id = refund_row.case_id
    customer_id = await _customer_id_for_case(session, case_id)
    session_factory = session_factory_for(session)

    async with hold_customer_advisory_lock(session_factory, customer_id):
        await lock_customer_for_update(session, customer_id)
        # expire_due_refunds_for_customer also takes the customer lock;
        # kept deliberately so the helper stays correct without a prior lock.
        reopened_case_ids = await expire_due_refunds_for_customer(
            session,
            customer_id=customer_id,
        )

        refund_row = await require_refund_by_id(session, refund_id)
        await session.refresh(refund_row)
        if refund_row.status is not RefundStatus.PENDING_APPROVAL:
            return OperatorOutcome(
                kind=OperatorResultKind.CONFLICT,
                case_id=case_id,
                refund_id=refund_id,
                refund_status=refund_row.status,
                reopened_case_ids=reopened_case_ids,
            )

        order_id = refund_row.order_id

        if not await transition_pending_refund_status(
            session,
            refund_id,
            RefundStatus.REJECTED,
            intent_binding=RefundIntentBinding(
                order_id=order_id,
                case_id=case_id,
                amount=refund_row.amount,
            ),
        ):
            await session.refresh(refund_row)
            return OperatorOutcome(
                kind=OperatorResultKind.CONFLICT,
                case_id=case_id,
                refund_id=refund_id,
                refund_status=refund_row.status,
                reopened_case_ids=reopened_case_ids,
            )

        await append_refund_rejected(
            session,
            customer_id=customer_id,
            case_id=case_id,
            order_id=order_id,
            refund_id=refund_id,
            operator_id=operator_id,
            reason=reason,
        )

        return OperatorOutcome(
            kind=OperatorResultKind.REJECTED,
            case_id=case_id,
            refund_id=refund_id,
            refund_status=RefundStatus.REJECTED,
            reopened_case_ids=reopened_case_ids,
        )


__all__ = [
    "approve_refund",
    "lock_customer_for_update",
    "reject_refund",
    "require_refund_by_id",
]
