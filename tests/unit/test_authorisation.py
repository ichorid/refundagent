"""Unit tests for single-use Authorisation capability."""

import pytest

from saferefund.actions.models import GetOrders, LinkOrder, SendReply
from saferefund.policy.authorisation import Authorisation, AuthorisationError
from saferefund.policy.checks import ObligationId, applicable_obligations
from saferefund.policy.policy import authorise
from saferefund.policy.verdicts import Allow, Deny
from tests.unit.policy_helpers import customer_summary, rule_context


def test_authorise_mints_authorisation_for_allow() -> None:
    action = GetOrders(action="get_orders")
    ctx = rule_context()
    result = authorise(ctx, action)
    assert isinstance(result, Authorisation)
    assert result.case_id == "case_test"
    assert result.action is action
    assert result.obligations_discharged == applicable_obligations(action)
    assert ObligationId.VERIFIED in result.obligations_discharged


def test_authorise_returns_deny_unchanged() -> None:
    action = GetOrders(action="get_orders")
    ctx = rule_context(customer=customer_summary(verified=False))
    result = authorise(ctx, action)
    assert isinstance(result, Deny)
    assert result.rule == "R_VERIFIED"


def test_authorisation_spend_accepts_matching_proposal() -> None:
    action = SendReply(action="send_reply", subject="Hi", body="There")
    auth = Authorisation(
        case_id="case_a",
        action=action,
        obligations_discharged=frozenset({ObligationId.CASE_ACTIONABLE}),
    )
    auth.spend(case_id="case_a", action=action)


def test_authorisation_rejects_case_or_action_mismatch() -> None:
    action = SendReply(action="send_reply", subject="Hi", body="There")
    other = SendReply(action="send_reply", subject="Hi", body="There")
    auth = Authorisation(
        case_id="case_a",
        action=action,
        obligations_discharged=frozenset(),
    )
    with pytest.raises(AuthorisationError, match="does not match"):
        auth.spend(case_id="case_b", action=action)
    with pytest.raises(AuthorisationError, match="does not match"):
        auth.spend(case_id="case_a", action=other)


def test_authorisation_rejects_double_spend() -> None:
    action = LinkOrder(action="link_order", order_id="ORD-1001")
    auth = Authorisation(
        case_id="case_a",
        action=action,
        obligations_discharged=frozenset(),
    )
    auth.spend(case_id="case_a", action=action)
    with pytest.raises(AuthorisationError, match="already spent"):
        auth.spend(case_id="case_a", action=action)


def test_allow_verdict_is_not_authorisation() -> None:
    assert not isinstance(Allow(), Authorisation)
