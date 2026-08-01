"""Unit tests for individual policy checks."""

from __future__ import annotations

from decimal import Decimal
from math import nan
from typing import Literal, cast

import pytest
from pydantic import BaseModel, ConfigDict

from saferefund.actions.models import (
    Action,
    GetOrders,
    LinkOrder,
    ProposeRefund,
)
from saferefund.config import DENIAL_LOOP_THRESHOLD
from saferefund.domain.enums import CaseStatus
from saferefund.policy.checks import (
    approval_if_over_threshold,
    deny_if_already_verified,
    deny_if_amount_unsound,
    deny_if_case_not_actionable,
    deny_if_exceeds_remainder,
    deny_if_no_linked_order,
    deny_if_open_refund,
    deny_if_order_not_owned,
    deny_if_unverified,
    escalate_if_denial_loop,
)
from saferefund.policy.policy import evaluate
from saferefund.policy.verdicts import (
    CONTINUE,
    Allow,
    Deny,
    ForceEscalate,
    RequireApproval,
)
from saferefund.repositories.seed import ORD_1001_ID, ORD_1003_ID, ORD_2001_ID
from tests.unit.policy_helpers import (
    open_case_summary,
    order_summary,
    rule_context,
    tom_unverified_context,
)


def test_deny_if_case_not_actionable_closed_case() -> None:
    ctx = rule_context(
        case=open_case_summary(status=CaseStatus.CLOSED),
    )
    result = deny_if_case_not_actionable(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_CASE_NOT_ACTIONABLE"


def test_deny_if_case_not_actionable_open_case() -> None:
    result = deny_if_case_not_actionable(rule_context())
    assert result is CONTINUE


def test_escalate_if_denial_loop_at_threshold() -> None:
    ctx = rule_context(
        case=open_case_summary(consecutive_denials=DENIAL_LOOP_THRESHOLD),
    )
    result = escalate_if_denial_loop(ctx)
    assert isinstance(result, ForceEscalate)
    assert result.rule == "R_DENIAL_LOOP"


def test_escalate_if_denial_loop_below_threshold() -> None:
    ctx = rule_context(
        case=open_case_summary(consecutive_denials=DENIAL_LOOP_THRESHOLD - 1),
    )
    assert escalate_if_denial_loop(ctx) is CONTINUE


def test_deny_if_unverified_blocks_refund() -> None:
    ctx = tom_unverified_context()
    result = deny_if_unverified(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_VERIFIED"
    assert "request_verification" in result.agent_reason


def test_deny_if_unverified_denies_unverified_customer() -> None:
    ctx = tom_unverified_context()
    result = deny_if_unverified(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_VERIFIED"


def test_deny_if_unverified_allows_verified_customer() -> None:
    ctx = rule_context()
    assert deny_if_unverified(ctx) is CONTINUE


def test_deny_if_already_verified_blocks_repeat_request() -> None:
    ctx = rule_context()
    result = deny_if_already_verified(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_ALREADY_VERIFIED"


def test_deny_if_already_verified_allows_unverified_customer() -> None:
    ctx = tom_unverified_context()
    assert deny_if_already_verified(ctx) is CONTINUE


def test_deny_if_order_not_owned_rejects_foreign_order() -> None:
    ctx = rule_context(customer_order_ids=frozenset({ORD_1001_ID}))
    action = LinkOrder(action="link_order", order_id=ORD_2001_ID)
    result = deny_if_order_not_owned(ctx, action)
    assert isinstance(result, Deny)
    assert result.rule == "R_ORDER_OWNERSHIP"


def test_deny_if_order_not_owned_allows_owned_order() -> None:
    ctx = rule_context(customer_order_ids=frozenset({ORD_1001_ID}))
    action = LinkOrder(action="link_order", order_id=ORD_1001_ID)
    assert deny_if_order_not_owned(ctx, action) is CONTINUE


def test_deny_if_no_linked_order_without_link() -> None:
    ctx = rule_context(case=open_case_summary(linked_order_id=None))
    result = deny_if_no_linked_order(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_NO_LINKED_ORDER"


def test_deny_if_no_linked_order_with_link() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1001_ID),
        linked_order=order_summary(),
    )
    assert deny_if_no_linked_order(ctx) is CONTINUE


@pytest.mark.parametrize(
    "amount",
    [
        Decimal("nan"),
        Decimal("inf"),
        Decimal("-inf"),
        Decimal(0),
        Decimal("-1.00"),
        Decimal("10.001"),
    ],
)
def test_deny_if_amount_unsound_rejects_invalid_amounts(amount: Decimal) -> None:
    action = ProposeRefund.model_construct(action="propose_refund", amount=amount)
    result = deny_if_amount_unsound(action)
    assert isinstance(result, Deny)
    assert result.rule == "R_AMOUNT_SANE"


def test_deny_if_amount_unsound_accepts_valid_amount() -> None:
    action = ProposeRefund(action="propose_refund", amount=Decimal("10.50"))
    assert deny_if_amount_unsound(action) is CONTINUE


def test_deny_if_amount_unsound_checks_finiteness_before_comparison() -> None:
    action = ProposeRefund.model_construct(
        action="propose_refund",
        amount=Decimal(nan),
    )
    result = deny_if_amount_unsound(action)
    assert isinstance(result, Deny)
    assert result.rule == "R_AMOUNT_SANE"


def test_deny_if_open_refund_blocks_when_open() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1001_ID),
        linked_order=order_summary(has_open_refund=True),
    )
    result = deny_if_open_refund(ctx)
    assert isinstance(result, Deny)
    assert result.rule == "R_OPEN_REFUND"


def test_deny_if_open_refund_allows_when_closed() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1001_ID),
        linked_order=order_summary(has_open_refund=False),
    )
    assert deny_if_open_refund(ctx) is CONTINUE


def test_deny_if_exceeds_remainder_blocks_over_limit() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1001_ID),
        linked_order=order_summary(total=Decimal("100.00")),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("100.01"))
    result = deny_if_exceeds_remainder(ctx, action)
    assert isinstance(result, Deny)
    assert result.rule == "R_REMAINDER"


def test_deny_if_exceeds_remainder_allows_within_remainder() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1001_ID),
        linked_order=order_summary(total=Decimal("100.00")),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("100.00"))
    assert deny_if_exceeds_remainder(ctx, action) is CONTINUE


def test_approval_if_over_threshold_requires_approval() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("201.00"))
    result = approval_if_over_threshold(ctx, action)
    assert isinstance(result, RequireApproval)
    assert result.rule == "R_THRESHOLD"


def test_approval_if_over_threshold_allows_at_threshold() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("200.00"))
    assert approval_if_over_threshold(ctx, action) is CONTINUE


def test_evaluate_allows_get_orders_on_open_verified_case() -> None:
    verdict = evaluate(rule_context(), GetOrders(action="get_orders"))
    assert isinstance(verdict, Allow)


def test_evaluate_cumulative_threshold_requires_approval_not_auto_allow() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("201.00"))
    verdict = evaluate(ctx, action)
    assert isinstance(verdict, RequireApproval)
    assert verdict.rule == "R_THRESHOLD"


def test_evaluate_open_refund_precedes_remainder_and_threshold() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
            has_open_refund=True,
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("900.00"))
    verdict = evaluate(ctx, action)
    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_OPEN_REFUND"


def test_evaluate_remainder_precedes_threshold() -> None:
    ctx = rule_context(
        case=open_case_summary(linked_order_id=ORD_1003_ID),
        linked_order=order_summary(
            order_id=ORD_1003_ID,
            total=Decimal("780.00"),
            refunded_total=Decimal("300.00"),
        ),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("900.00"))
    verdict = evaluate(ctx, action)
    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_REMAINDER"


def test_evaluate_uncovered_action_type_fails_closed() -> None:
    """Mutation: add a default empty ``Obligations`` entry for unknown action types."""

    class RescindRefund(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True)
        action: Literal["rescind_refund"] = "rescind_refund"

    verdict = evaluate(rule_context(), cast("Action", RescindRefund()))

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_EXHAUSTED"
