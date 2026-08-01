"""Trusted inputs available to pure policy checks."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from saferefund.projections.case import CaseSummary
from saferefund.projections.customer import CustomerSummary
from saferefund.projections.order import OrderSummary


@dataclass(frozen=True, slots=True)
class RuleContext:
    """Folded summaries and configuration for one policy evaluation."""

    now: datetime
    customer: CustomerSummary
    case: CaseSummary
    linked_order: OrderSummary | None
    customer_order_ids: frozenset[str]
    refund_approval_threshold: Decimal
    denial_loop_threshold: int


__all__ = ["RuleContext"]
