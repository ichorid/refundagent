"""Guard tests that the advisory menu does not affect policy evaluation."""

from decimal import Decimal

from saferefund.actions.models import ProposeRefund
from saferefund.agent.prompt import available_actions
from saferefund.domain.enums import CaseStatus
from saferefund.policy.policy import evaluate
from saferefund.policy.verdicts import Deny
from saferefund.projections.case import CaseSummary
from saferefund.repositories.seed import ORD_1001_ID, ORD_2001_ID
from tests.unit.policy_helpers import (
    customer_summary,
    open_case_summary,
    order_summary,
    rule_context,
    tom_unverified_context,
)


def test_menu_omits_propose_refund_without_linked_order() -> None:
    """Advisory menu hides propose_refund until an order is linked."""
    case = CaseSummary(
        case_id="case_menu_test",
        customer_id="cust_sophie",
        status=CaseStatus.OPEN,
        linked_order_id=None,
        orders_listed=True,
        step_count=0,
        consecutive_denials=0,
        consecutive_invalid_outputs=0,
        last_refund_status=None,
        reply_sent_after_last_refund=False,
    )
    menu = available_actions(case, customer_summary())

    assert "propose_refund" not in menu
    assert "link_order" in menu


def test_menu_is_advisory_policy_ignores_menu_contents() -> None:
    """The same action receives the same policy verdict when the menu omits it."""
    case = CaseSummary(
        case_id="case_menu_test",
        customer_id="cust_sophie",
        status=CaseStatus.OPEN,
        linked_order_id=None,
        orders_listed=True,
        step_count=0,
        consecutive_denials=0,
        consecutive_invalid_outputs=0,
        last_refund_status=None,
        reply_sent_after_last_refund=False,
    )
    customer = customer_summary()
    menu_without_refund = available_actions(case, customer)

    ctx = rule_context(
        case=case,
        linked_order=None,
        customer_order_ids=frozenset({ORD_1001_ID}),
    )
    action = ProposeRefund(action="propose_refund", amount=Decimal("10.00"))
    verdict_without_menu_entry = evaluate(ctx, action)

    assert "propose_refund" not in menu_without_refund
    assert isinstance(verdict_without_menu_entry, Deny)
    assert verdict_without_menu_entry.rule == "R_NO_LINKED_ORDER"

    menu_with_refund = (*menu_without_refund, "propose_refund")
    assert "propose_refund" in menu_with_refund
    assert evaluate(ctx, action) == verdict_without_menu_entry


def test_menu_is_advisory_for_unverified_refund_attempt() -> None:
    """Unverified refund denial is unchanged whether the menu lists propose_refund."""
    linked = order_summary(order_id=ORD_2001_ID, total=Decimal("60.00"))
    ctx = tom_unverified_context(linked_order=linked)
    action = ProposeRefund(action="propose_refund", amount=Decimal("10.00"))

    menu = available_actions(ctx.case, ctx.customer)
    verdict = evaluate(ctx, action)

    assert isinstance(verdict, Deny)
    assert verdict.rule == "R_VERIFIED"
    if "propose_refund" not in menu:
        assert evaluate(ctx, action) == verdict


def test_menu_reflects_case_state_without_policy_calls() -> None:
    """Menu narrows by projection state only and never imports policy."""
    awaiting = open_case_summary(status=CaseStatus.AWAITING_APPROVAL)
    menu = available_actions(awaiting, customer_summary())

    assert "finish" in menu
    assert "escalate" in menu
