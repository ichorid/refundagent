"""Parametrised policy decision table and fail-closed guards."""

import inspect
from decimal import Decimal
from typing import Literal, get_args

import pytest

from saferefund import config, policy
from saferefund.actions import (
    ACTION_MODEL_CLASSES,
    Action,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.models import CaseStatus
from saferefund.policy import (
    Allow,
    Deny,
    PolicyState,
    RequireApproval,
    decide,
)
from saferefund.policy import (
    Escalate as PolicyEscalate,
)

_BASE_STATE = {
    "case_status": CaseStatus.OPEN,
    "consecutive_denials": 0,
    "customer_verified": True,
    "owned_order_ids": frozenset({"ORD-1001", "ORD-1002"}),
    "linked_order_id": "ORD-1001",
    "linked_order_total": Decimal("249.00"),
    "linked_order_refunded": Decimal("0"),
    "linked_order_has_open_refund": False,
    "customer_refunded_total": Decimal("0"),
    "approval_threshold": config.REFUND_APPROVAL_THRESHOLD,
    "denial_loop_threshold": config.DENIAL_LOOP_THRESHOLD,
}


def _state(**overrides: object) -> PolicyState:
    return PolicyState(**{**_BASE_STATE, **overrides})


def _action_literal(cls: type) -> str:
    annotation = cls.model_fields["action"].annotation
    if isinstance(annotation, type(Literal["x"])):
        return get_args(annotation)[0]
    return str(annotation)


POLICY_TABLE: list[tuple[str, dict[str, object], Action, type, str | None]] = [
    (
        "R_CASE_NOT_OPEN",
        {"case_status": CaseStatus.CLOSED},
        SendReply(action="send_reply", subject="Hi", body="Hello"),
        Deny,
        "R_CASE_NOT_OPEN",
    ),
    (
        "R_DENIAL_LOOP",
        {"consecutive_denials": config.DENIAL_LOOP_THRESHOLD},
        Finish(action="finish", summary="done"),
        PolicyEscalate,
        "R_DENIAL_LOOP",
    ),
    (
        "R_UNVERIFIED_get_orders",
        {"customer_verified": False},
        GetOrders(action="get_orders"),
        Deny,
        "R_UNVERIFIED",
    ),
    (
        "R_ALREADY_VERIFIED",
        {},
        RequestVerification(action="request_verification"),
        Deny,
        "R_ALREADY_VERIFIED",
    ),
    (
        "R_NOT_OWNED",
        {"owned_order_ids": frozenset({"ORD-1002"})},
        LinkOrder(action="link_order", order_id="ORD-1001"),
        Deny,
        "R_NOT_OWNED",
    ),
    (
        "R_NO_LINKED_ORDER",
        {
            "linked_order_id": None,
            "linked_order_total": None,
            "linked_order_refunded": None,
        },
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        Deny,
        "R_NO_LINKED_ORDER",
    ),
    (
        "R_AMOUNT_zero",
        {},
        ProposeRefund(action="propose_refund", amount=Decimal("0")),
        Deny,
        "R_AMOUNT",
    ),
    (
        "R_OPEN_REFUND",
        {"linked_order_has_open_refund": True},
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        Deny,
        "R_OPEN_REFUND",
    ),
    (
        "R_REMAINDER",
        {"linked_order_refunded": Decimal("240.00")},
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        Deny,
        "R_REMAINDER",
    ),
    (
        "R_THRESHOLD",
        {
            "linked_order_total": Decimal("780.00"),
            "linked_order_refunded": Decimal("0"),
        },
        ProposeRefund(action="propose_refund", amount=Decimal("600.00")),
        RequireApproval,
        "R_THRESHOLD",
    ),
    ("allow_get_orders", {}, GetOrders(action="get_orders"), Allow, None),
    (
        "allow_link_order",
        {},
        LinkOrder(action="link_order", order_id="ORD-1001"),
        Allow,
        None,
    ),
    (
        "allow_propose_refund",
        {},
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        Allow,
        None,
    ),
    (
        "allow_send_reply",
        {},
        SendReply(action="send_reply", subject="Update", body="Thanks for waiting."),
        Allow,
        None,
    ),
    (
        "allow_request_verification",
        {"customer_verified": False},
        RequestVerification(action="request_verification"),
        Allow,
        None,
    ),
    (
        "allow_escalate",
        {},
        Escalate(action="escalate", reason="Need a human"),
        Allow,
        None,
    ),
    ("allow_finish", {}, Finish(action="finish", summary="Resolved"), Allow, None),
]


@pytest.mark.parametrize(
    ("name", "state_kwargs", "action", "expected_type", "expected_rule"),
    POLICY_TABLE,
)
def test_policy_decision_table(
    name: str,
    state_kwargs: dict[str, object],
    action: Action,
    expected_type: type,
    expected_rule: str | None,
) -> None:
    """One row per policy rule plus one Allow row per action type."""
    decision = decide(_state(**state_kwargs), action)
    assert isinstance(decision, expected_type)
    if expected_rule is not None:
        assert getattr(decision, "rule", None) == expected_rule


def test_every_action_type_has_an_allow_row() -> None:
    """Every action in the union must have an Allow row in the decision table."""
    allow_actions = {row[2].action for row in POLICY_TABLE if row[3] is Allow}
    union_actions = {_action_literal(cls) for cls in ACTION_MODEL_CLASSES}
    assert allow_actions == union_actions


def test_decide_never_defaults_to_allow() -> None:
    """decide() must close with assert_never, not a bare trailing return Allow()."""
    source = inspect.getsource(decide)
    assert "assert_never" in source
    assert "match action:" in source
    lines = source.splitlines()
    bare_returns = [line for line in lines if line == "    return Allow()"]
    assert bare_returns == []
    match_returns = [line for line in lines if line.strip() == "return Allow()"]
    assert len(match_returns) == 1
    assert match_returns[0].startswith("            ")


def test_policy_imports_no_effects() -> None:
    """policy.py must not import service, adapters, agent, api, or sqlalchemy."""
    forbidden = ("service", "adapters", "agent", "api", "sqlalchemy")
    source = inspect.getsource(policy)
    import_lines = [
        line.strip()
        for line in source.splitlines()
        if line.strip().startswith(("import ", "from "))
    ]
    joined = "\n".join(import_lines)
    for name in forbidden:
        assert name not in joined
