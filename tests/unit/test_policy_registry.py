"""Registry wiring tests for the obligation coverage table."""

from decimal import Decimal

import pytest

from saferefund.actions.models import (
    ACTION_MODEL_CLASSES,
    Action,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
    _ActionBase,
)
from saferefund.policy.checks import (
    ACTION_OBLIGATIONS,
    CANONICAL_ORDER,
    CHECKS,
    UNIVERSAL_OBLIGATIONS,
    ObligationId,
)
from saferefund.policy.verdicts import Allow
from tests.unit.policy_helpers import perform_checks, rule_context

_REPRESENTATIVE_ACTIONS: dict[type[_ActionBase], Action] = {
    GetOrders: GetOrders(action="get_orders"),
    LinkOrder: LinkOrder(action="link_order", order_id="ORD-TEST"),
    ProposeRefund: ProposeRefund(action="propose_refund", amount=Decimal("1.00")),
    RequestVerification: RequestVerification(action="request_verification"),
    SendReply: SendReply(action="send_reply", subject="Hi", body="There"),
    Escalate: Escalate(action="escalate", reason="test"),
    Finish: Finish(action="finish", summary="done"),
}


def representative_action(action_type: type[_ActionBase]) -> Action:
    try:
        return _REPRESENTATIVE_ACTIONS[action_type]
    except KeyError as exc:
        msg = f"no representative action for {action_type.__name__}"
        raise ValueError(msg) from exc


def test_canonical_order_lists_every_obligation_once() -> None:
    """Mutation: append a duplicate ``ObligationId`` to ``CANONICAL_ORDER``."""
    assert len(CANONICAL_ORDER) == len(set(CANONICAL_ORDER))
    assert set(CANONICAL_ORDER) == set(ObligationId)


def test_checks_registry_covers_canonical_order() -> None:
    """Mutation: drop one entry from ``CHECKS``."""
    assert set(CHECKS) == set(CANONICAL_ORDER)


@pytest.mark.parametrize("action_type", ACTION_OBLIGATIONS)
def test_applicable_checks_accept_representative_action(
    action_type: type,
) -> None:
    """Mutation: wire ``ORDER_OWNED`` onto ``GetOrders`` in ``ACTION_OBLIGATIONS``."""
    action = representative_action(action_type)
    ctx = rule_context()
    applicable = (
        frozenset(UNIVERSAL_OBLIGATIONS) | ACTION_OBLIGATIONS[action_type].required
    )
    for obligation_id in CANONICAL_ORDER:
        if obligation_id in applicable:
            CHECKS[obligation_id](ctx, action)


def test_miswired_order_owned_check_rejects_wrong_action_type() -> None:
    """Mutation: remove the ``isinstance`` guard in ``_check_order_owned``."""
    with pytest.raises(TypeError, match="order_owned"):
        CHECKS[ObligationId.ORDER_OWNED](
            rule_context(),
            GetOrders(action="get_orders"),
        )


def test_miswired_threshold_check_rejects_wrong_action_type() -> None:
    """Mutation: remove the ``isinstance`` guard in ``_check_threshold``."""
    with pytest.raises(TypeError, match="threshold"):
        CHECKS[ObligationId.THRESHOLD](
            rule_context(),
            LinkOrder(action="link_order", order_id="ORD-1001"),
        )


def test_perform_checks_never_yields_allow() -> None:
    """Mutation: return ``Allow()`` from any obligation check."""
    ctx = rule_context()
    for action_type in ACTION_OBLIGATIONS:
        action = representative_action(action_type)
        for result in perform_checks(ctx, action):
            assert not isinstance(result, Allow)


def test_every_action_model_class_has_coverage() -> None:
    """Mutation: omit ``Finish`` from ``ACTION_OBLIGATIONS``."""
    assert set(ACTION_MODEL_CLASSES) == set(ACTION_OBLIGATIONS)


@pytest.mark.parametrize(
    "obligation_id",
    [
        ObligationId.AMOUNT_SANE,
        ObligationId.NO_OPEN_REFUND,
        ObligationId.WITHIN_REMAINDER,
        ObligationId.THRESHOLD,
    ],
)
def test_refund_only_obligations_reject_non_refund_actions(
    obligation_id: ObligationId,
) -> None:
    """Mutation: attach a refund-only obligation to ``SendReply``."""
    wrong_action = representative_action(GetOrders)
    with pytest.raises(TypeError, match=obligation_id.value):
        CHECKS[obligation_id](rule_context(), wrong_action)
