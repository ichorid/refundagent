"""Shared fixtures and helpers for policy unit tests."""

from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal

from saferefund.actions.models import Action
from saferefund.config import DENIAL_LOOP_THRESHOLD, REFUND_APPROVAL_THRESHOLD
from saferefund.domain.enums import CaseStatus
from saferefund.policy.checks import CANONICAL_ORDER, CHECKS, applicable_obligations
from saferefund.policy.context import RuleContext
from saferefund.policy.verdicts import CheckResult
from saferefund.projections.case import CaseSummary
from saferefund.projections.customer import CustomerSummary
from saferefund.projections.order import OrderSummary
from saferefund.repositories.seed import (
    ORD_1001_ID,
    ORD_1003_ID,
    ORD_2001_ID,
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    TOM_CUSTOMER_ID,
    TOM_EMAIL,
)

BASE_TIME = datetime(2030, 6, 1, 12, 0, tzinfo=UTC)
TEST_CASE_ID = "case_test"


def perform_checks(
    ctx: RuleContext,
    action: Action,
) -> Iterator[CheckResult]:
    """Yield policy results in normative precedence order."""
    applicable = applicable_obligations(action)
    for obligation_id in CANONICAL_ORDER:
        if obligation_id in applicable:
            yield CHECKS[obligation_id](ctx, action)


def open_case_summary(
    *,
    case_id: str = TEST_CASE_ID,
    customer_id: str = SOPHIE_CUSTOMER_ID,
    linked_order_id: str | None = None,
    consecutive_denials: int = 0,
    status: CaseStatus = CaseStatus.OPEN,
) -> CaseSummary:
    return CaseSummary(
        case_id=case_id,
        customer_id=customer_id,
        status=status,
        linked_order_id=linked_order_id,
        orders_listed=False,
        step_count=0,
        consecutive_denials=consecutive_denials,
        consecutive_invalid_outputs=0,
        last_refund_status=None,
        reply_sent_after_last_refund=False,
    )


def customer_summary(
    *,
    customer_id: str = SOPHIE_CUSTOMER_ID,
    email: str = SOPHIE_EMAIL,
    verified: bool = True,
) -> CustomerSummary:
    return CustomerSummary(
        customer_id=customer_id,
        email=email,
        verified=verified,
    )


def order_summary(
    *,
    order_id: str = ORD_1001_ID,
    customer_id: str = SOPHIE_CUSTOMER_ID,
    total: Decimal = Decimal("249.00"),
    refunded_total: Decimal = Decimal(0),
    has_open_refund: bool = False,
) -> OrderSummary:
    return OrderSummary(
        order_id=order_id,
        customer_id=customer_id,
        total=total,
        refunded_total=refunded_total,
        has_open_refund=has_open_refund,
    )


def rule_context(  # noqa: PLR0913
    *,
    customer: CustomerSummary | None = None,
    case: CaseSummary | None = None,
    linked_order: OrderSummary | None = None,
    customer_order_ids: frozenset[str] | None = None,
    refund_approval_threshold: Decimal = REFUND_APPROVAL_THRESHOLD,
    denial_loop_threshold: int = DENIAL_LOOP_THRESHOLD,
) -> RuleContext:
    resolved_customer = customer or customer_summary()
    resolved_case = case or open_case_summary()
    return RuleContext(
        now=BASE_TIME,
        customer=resolved_customer,
        case=resolved_case,
        linked_order=linked_order,
        customer_order_ids=customer_order_ids or frozenset({ORD_1001_ID, ORD_1003_ID}),
        refund_approval_threshold=refund_approval_threshold,
        denial_loop_threshold=denial_loop_threshold,
    )


def tom_unverified_context(
    *,
    linked_order: OrderSummary | None = None,
    customer_order_ids: frozenset[str] | None = None,
) -> RuleContext:
    return rule_context(
        customer=customer_summary(
            customer_id=TOM_CUSTOMER_ID,
            email=TOM_EMAIL,
            verified=False,
        ),
        case=open_case_summary(
            customer_id=TOM_CUSTOMER_ID,
            linked_order_id=ORD_2001_ID if linked_order else None,
        ),
        linked_order=linked_order,
        customer_order_ids=customer_order_ids or frozenset({ORD_2001_ID}),
    )
