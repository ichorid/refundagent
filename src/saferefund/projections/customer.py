"""Customer-scoped control-state projection."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from saferefund.domain.events import EventType
from saferefund.projections.types import CustomerSeed, FoldableEvent


@dataclass(frozen=True, slots=True)
class CustomerSummary:
    """Folded customer verification state from canonical events."""

    customer_id: str
    email: str
    verified: bool


def _customer_scoped_events(
    customer_id: str,
    events: Sequence[FoldableEvent],
) -> list[FoldableEvent]:
    return [
        event
        for event in events
        if event.customer_id == customer_id
        and event.event_type is EventType.CUSTOMER_VERIFIED
    ]


def project_customer_summary(
    customer_seed: CustomerSeed,
    events: Sequence[FoldableEvent],
    now: datetime,  # noqa: ARG001
) -> CustomerSummary:
    """Fold customer-scoped verification events in ascending sequence order."""
    verified = bool(_customer_scoped_events(customer_seed.customer_id, events))
    return CustomerSummary(
        customer_id=customer_seed.customer_id,
        email=customer_seed.email,
        verified=verified,
    )
