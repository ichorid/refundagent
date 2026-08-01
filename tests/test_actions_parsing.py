"""Strict typed action parsing and rejection."""

import json
from decimal import Decimal

from saferefund.actions import GetOrders, LinkOrder, ProposeRefund
from saferefund.agent import parse_action


def test_parse_valid_get_orders() -> None:
    """Valid JSON maps to the correct typed action."""
    parsed = parse_action(json.dumps({"action": "get_orders"}))
    assert isinstance(parsed, GetOrders)


def test_parse_valid_propose_refund() -> None:
    """Decimal amounts with two places parse cleanly."""
    parsed = parse_action(json.dumps({"action": "propose_refund", "amount": "24.50"}))
    assert isinstance(parsed, ProposeRefund)
    assert parsed.amount == Decimal("24.50")


def test_parse_rejects_extra_fields() -> None:
    """extra=forbid rejects unknown keys."""
    parsed = parse_action(
        json.dumps({"action": "link_order", "order_id": "ORD-1001", "evil": True})
    )
    assert not isinstance(parsed, LinkOrder)
    assert hasattr(parsed, "message")


def test_parse_rejects_invalid_json() -> None:
    """Malformed JSON returns ParseError instead of raising."""
    parsed = parse_action("{not json")
    assert hasattr(parsed, "message")
    assert "malformed JSON" in parsed.message


def test_parse_rejects_too_many_decimal_places() -> None:
    """Amounts with more than two decimal places are rejected at parse time."""
    parsed = parse_action(json.dumps({"action": "propose_refund", "amount": "1.001"}))
    assert hasattr(parsed, "message")
    assert "two decimal" in parsed.message


def test_parse_rejects_unknown_action_type() -> None:
    """Unknown action discriminators fail validation."""
    parsed = parse_action(json.dumps({"action": "wire_money", "amount": "1.00"}))
    assert hasattr(parsed, "message")
