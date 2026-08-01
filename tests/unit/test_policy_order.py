"""Policy precedence and ordering guard tests."""

from decimal import Decimal

import pytest

from saferefund.actions.models import (
    Action,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.config import DENIAL_LOOP_THRESHOLD, REFUND_APPROVAL_THRESHOLD
from saferefund.policy.context import RuleContext
from saferefund.policy.policy import evaluate
from saferefund.policy.verdicts import Allow, Deny, ForceEscalate, RequireApproval
from saferefund.repositories.seed import ORD_1001_ID, ORD_1003_ID, ORD_2001_ID
from tests.unit.policy_helpers import (
    open_case_summary,
    order_summary,
    perform_checks,
    rule_context,
    tom_unverified_context,
)


def _assert_refusing_checks_precede_require_approval(
    ctx: RuleContext,
    action: Action,
) -> None:
    """Every Deny/ForceEscalate must appear before the first RequireApproval."""
    saw_require_approval = False
    for result in perform_checks(ctx, action):
        if isinstance(result, RequireApproval):
            saw_require_approval = True
            continue
        if saw_require_approval and isinstance(result, (Deny, ForceEscalate)):
            verdict_kind = type(result).__name__
            rule = getattr(result, "rule", None)
            msg = f"{verdict_kind}({rule}) appears after RequireApproval"
            raise AssertionError(msg)
    assert saw_require_approval


@pytest.mark.parametrize(
    ("ctx", "action"),
    [
        pytest.param(
            rule_context(
                case=open_case_summary(linked_order_id=ORD_2001_ID),
                linked_order=order_summary(
                    order_id=ORD_2001_ID,
                    total=Decimal("800.00"),
                    refunded_total=Decimal("300.00"),
                ),
                customer_order_ids=frozenset({ORD_2001_ID}),
            ),
            ProposeRefund(action="propose_refund", amount=Decimal("201.00")),
            id="verified-propose-over-threshold",
        ),
        pytest.param(
            tom_unverified_context(
                linked_order=order_summary(
                    order_id=ORD_2001_ID,
                    total=Decimal("800.00"),
                ),
            ),
            ProposeRefund(action="propose_refund", amount=Decimal("800.00")),
            id="unverified-propose-refund",
        ),
        pytest.param(
            rule_context(
                case=open_case_summary(
                    linked_order_id=ORD_1003_ID,
                    consecutive_denials=DENIAL_LOOP_THRESHOLD,
                ),
                linked_order=order_summary(
                    order_id=ORD_1003_ID,
                    total=Decimal("780.00"),
                ),
                customer_order_ids=frozenset({ORD_1003_ID}),
            ),
            ProposeRefund(action="propose_refund", amount=Decimal("600.00")),
            id="denial-loop-propose-refund",
        ),
        pytest.param(
            rule_context(
                case=open_case_summary(linked_order_id=ORD_1003_ID),
                linked_order=order_summary(
                    order_id=ORD_1003_ID,
                    total=Decimal("780.00"),
                    refunded_total=Decimal("300.00"),
                    has_open_refund=True,
                ),
                customer_order_ids=frozenset({ORD_1003_ID}),
            ),
            ProposeRefund(action="propose_refund", amount=Decimal("900.00")),
            id="open-refund-propose-refund",
        ),
        pytest.param(
            rule_context(
                case=open_case_summary(linked_order_id=None),
                customer_order_ids=frozenset({ORD_1001_ID, ORD_1003_ID}),
            ),
            LinkOrder(action="link_order", order_id=ORD_1003_ID),
            id="link-order-owned",
        ),
        pytest.param(
            rule_context(),
            GetOrders(action="get_orders"),
            id="get-orders",
        ),
        pytest.param(
            tom_unverified_context(),
            RequestVerification(action="request_verification"),
            id="unverified-request-verification",
        ),
        pytest.param(
            rule_context(
                case=open_case_summary(linked_order_id=ORD_1001_ID),
                linked_order=order_summary(order_id=ORD_1001_ID),
            ),
            SendReply(action="send_reply", subject="Hi", body="Update"),
            id="verified-send-reply",
        ),
    ],
)
def test_policy_order_contract_refusing_checks_precede_require_approval(
    ctx: RuleContext,
    action: Action,
) -> None:
    """Every Deny/ForceEscalate check appears before the first RequireApproval."""
    saw_require_approval = False
    for result in perform_checks(ctx, action):
        if isinstance(result, RequireApproval):
            saw_require_approval = True
            continue
        if saw_require_approval and isinstance(result, (Deny, ForceEscalate)):
            verdict_kind = type(result).__name__
            rule = getattr(result, "rule", None)
            msg = f"{verdict_kind}({rule}) appears after RequireApproval"
            raise AssertionError(msg)


def test_policy_order_contract_require_approval_is_reachable_in_money_path() -> None:
    """One money-moving context must reach RequireApproval in perform_checks."""
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_2001_ID),
        linked_order=order_summary(
            order_id=ORD_2001_ID,
            total=Decimal("800.00"),
            refunded_total=Decimal("300.00"),
        ),
        customer_order_ids=frozenset({ORD_2001_ID}),
    )
    _assert_refusing_checks_precede_require_approval(
        ctx,
        ProposeRefund(action="propose_refund", amount=Decimal("201.00")),
    )


def test_policy_order_contract_unverified_refund_denied_before_threshold() -> None:
    """Tom proposing 800 must hit R_VERIFIED, not R_THRESHOLD RequireApproval."""
    ctx = tom_unverified_context(
        linked_order=order_summary(
            order_id=ORD_2001_ID,
            total=Decimal("800.00"),
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("800.00"))
    verdict = evaluate(ctx, action)
    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_VERIFIED"


def test_threshold_uses_cumulative_executed_total() -> None:
    """First 300 auto-allows; a second 300 cumulative proposal needs approval."""
    first_ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_2001_ID),
        linked_order=order_summary(
            order_id=ORD_2001_ID,
            total=Decimal("800.00"),
            refunded_total=Decimal(0),
        ),
        customer_order_ids=frozenset({ORD_2001_ID}),
        refund_approval_threshold=REFUND_APPROVAL_THRESHOLD,
    )
    first = evaluate(
        first_ctx,
        ProposeRefund(action="propose_refund", amount=Decimal("300.00")),
    )
    assert isinstance(first, Allow)

    second_ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_2001_ID),
        linked_order=order_summary(
            order_id=ORD_2001_ID,
            total=Decimal("800.00"),
            refunded_total=Decimal("300.00"),
        ),
        customer_order_ids=frozenset({ORD_2001_ID}),
        refund_approval_threshold=REFUND_APPROVAL_THRESHOLD,
    )
    second = evaluate(
        second_ctx,
        ProposeRefund(action="propose_refund", amount=Decimal("300.00")),
    )
    assert isinstance(second, RequireApproval)
    assert second.rule == "R_THRESHOLD"
