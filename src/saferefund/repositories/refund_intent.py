"""Canonical refund_proposed evidence and row/intent integrity checks."""

from __future__ import annotations

from typing import TYPE_CHECKING

from saferefund.domain.events import EventType
from saferefund.domain.payloads import RefundProposedPayload
from saferefund.repositories.events import StoredEvent, load_case_events

if TYPE_CHECKING:
    from decimal import Decimal

    from sqlalchemy.ext.asyncio import AsyncSession

    from saferefund.domain.tables import RefundRow


class RefundIntentIntegrityError(RuntimeError):
    """Raised when a refund row disagrees with canonical proposal evidence."""


async def load_canonical_refund_proposed_event(
    session: AsyncSession,
    *,
    case_id: str,
    refund_id: str,
) -> StoredEvent:
    """Return the sole refund_proposed event for one refund id within a case."""
    case_events = await load_case_events(session, case_id)
    matching_events: list[StoredEvent] = []
    for event in case_events:
        if event.event_type is not EventType.REFUND_PROPOSED:
            continue
        proposed_payload = RefundProposedPayload.model_validate(event.payload)
        if proposed_payload.refund_id != refund_id:
            continue
        matching_events.append(event)

    if not matching_events:
        message = (
            "refund intent integrity failure: no refund_proposed evidence "
            f"for {refund_id}"
        )
        raise RefundIntentIntegrityError(message)
    if len(matching_events) > 1:
        message = (
            "refund intent integrity failure: ambiguous refund_proposed evidence "
            f"for {refund_id}"
        )
        raise RefundIntentIntegrityError(message)
    return matching_events[0]


async def validate_refund_intent_against_proposed_evidence(
    session: AsyncSession,
    refund_row: RefundRow,
    *,
    customer_id: str,
) -> RefundProposedPayload:
    """Fail closed when row identity or amount disagrees with proposal evidence."""
    proposed_event = await load_canonical_refund_proposed_event(
        session,
        case_id=refund_row.case_id,
        refund_id=refund_row.id,
    )
    proposed_payload = RefundProposedPayload.model_validate(proposed_event.payload)

    mismatches: list[str] = []
    if proposed_event.customer_id != customer_id:
        mismatches.append("customer_id")
    if proposed_event.customer_id != refund_row.customer_id:
        mismatches.append("refund_row.customer_id")
    if proposed_event.case_id != refund_row.case_id:
        mismatches.append("case_id")
    if proposed_event.order_id != refund_row.order_id:
        mismatches.append("order_id")
    if proposed_payload.refund_id != refund_row.id:
        mismatches.append("refund_id")
    if proposed_payload.amount != refund_row.amount:
        mismatches.append("amount")

    if mismatches:
        fields = ", ".join(sorted(mismatches))
        message = (
            "refund intent integrity failure: refund row disagrees with "
            f"refund_proposed evidence ({fields}) for {refund_row.id}"
        )
        raise RefundIntentIntegrityError(message)

    return proposed_payload


def refund_payment_amount_from_evidence(
    proposed_payload: RefundProposedPayload,
) -> Decimal:
    """Return the Decimal amount that payment must use for one refund."""
    return proposed_payload.amount


__all__ = [
    "RefundIntentIntegrityError",
    "load_canonical_refund_proposed_event",
    "refund_payment_amount_from_evidence",
    "validate_refund_intent_against_proposed_evidence",
]
