from decimal import Decimal

import pytest

from saferefund import config
from saferefund.actions.models import (
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.agent.parsing import ParseFailure, ParseSuccess, parse


@pytest.mark.parametrize(
    ("raw_output", "expected_action"),
    [
        ('{"action": "get_orders"}', GetOrders(action="get_orders")),
        (
            '{"action": "link_order", "order_id": "ORD-1001"}',
            LinkOrder(action="link_order", order_id="ORD-1001"),
        ),
        (
            '{"action": "propose_refund", "amount": "249.00"}',
            ProposeRefund(action="propose_refund", amount=Decimal("249.00")),
        ),
        (
            '{"action": "propose_refund", "amount": "10.5"}',
            ProposeRefund(action="propose_refund", amount=Decimal("10.5")),
        ),
        (
            '{"action": "send_reply", "subject": "Update", "body": "Hello"}',
            SendReply(action="send_reply", subject="Update", body="Hello"),
        ),
        (
            '{"action": "request_verification"}',
            RequestVerification(action="request_verification"),
        ),
        (
            '{"action": "escalate", "reason": "Need a human"}',
            Escalate(action="escalate", reason="Need a human"),
        ),
        (
            '{"action": "finish", "summary": "Resolved"}',
            Finish(action="finish", summary="Resolved"),
        ),
    ],
)
def test_parse_accepts_valid_actions(
    raw_output: str,
    expected_action: object,
) -> None:
    result = parse(raw_output)

    assert isinstance(result, ParseSuccess)
    assert result.action == expected_action


@pytest.mark.parametrize(
    "raw_output",
    [
        "",
        "   ",
        "{not json",
        '["get_orders"]',
        '"get_orders"',
        '{"action": "refund_everything"}',
        '{"action": "get_orders", "customer_id": "cust_sophie"}',
        '{"action": "send_reply", "subject": "Hi", "body": "There", "to": "a@b.com"}',
        '{"action": "propose_refund", "amount": "10.001"}',
        '{"action": "propose_refund", "amount": "NaN"}',
        '{"action": "propose_refund", "amount": "Infinity"}',
        '{"action": "propose_refund", "amount": "-Infinity"}',
    ],
)
def test_parse_rejects_invalid_outputs(raw_output: str) -> None:
    result = parse(raw_output)

    assert isinstance(result, ParseFailure)
    assert result.error


def test_parse_never_raises_on_hostile_input() -> None:
    hostile_inputs = [
        "\x00",
        '{"action": "get_orders"' + "}" * 10_000,
        '{"action": null}',
        '{"action": 1}',
    ]

    for hostile_input in hostile_inputs:
        result = parse(hostile_input)
        assert isinstance(result, (ParseSuccess, ParseFailure))


@pytest.mark.parametrize(
    "raw_output",
    [
        (
            '{"action": "send_reply", "subject": "'
            + ("s" * (config.ACTION_TEXT_FIELD_MAX_LENGTH + 1))
            + '", "body": "ok"}'
        ),
        (
            '{"action": "send_reply", "subject": "ok", "body": "'
            + ("b" * (config.ACTION_TEXT_FIELD_MAX_LENGTH + 1))
            + '"}'
        ),
        (
            '{"action": "escalate", "reason": "'
            + ("r" * (config.ACTION_TEXT_FIELD_MAX_LENGTH + 1))
            + '"}'
        ),
        (
            '{"action": "finish", "summary": "'
            + ("f" * (config.ACTION_TEXT_FIELD_MAX_LENGTH + 1))
            + '"}'
        ),
        (
            '{"action": "link_order", "order_id": "'
            + ("O" * (config.ACTION_ORDER_ID_MAX_LENGTH + 1))
            + '"}'
        ),
    ],
    ids=[
        "reply_subject",
        "reply_body",
        "escalation_reason",
        "finish_summary",
        "order_id",
    ],
)
def test_oversized_action_text_is_a_parse_failure(raw_output: str) -> None:
    result = parse(raw_output)

    assert isinstance(result, ParseFailure)
    assert result.error


def test_parse_preserves_unquantised_decimal_scale_within_limit() -> None:
    result = parse('{"action": "propose_refund", "amount": "10.50"}')

    assert isinstance(result, ParseSuccess)
    assert isinstance(result.action, ProposeRefund)
    assert result.action.amount == Decimal("10.50")
    assert result.action.amount.as_tuple().exponent == -2
