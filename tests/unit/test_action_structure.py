from saferefund.actions.models import (
    ACTION_MODEL_CLASSES,
    LinkOrder,
    ProposeRefund,
    SendReply,
)
from saferefund.agent.parsing import ParseFailure, parse
from tests.unit.action_structure_helpers import (
    all_action_models_forbid_extra_fields,
    forbidden_identity_field_names,
    propose_refund_has_amount_only,
)


def test_no_identity_or_recipient_fields_on_action_models() -> None:
    assert forbidden_identity_field_names() == ()


def test_propose_refund_exposes_amount_only_besides_discriminator() -> None:
    assert propose_refund_has_amount_only()
    assert "order_id" not in ProposeRefund.model_fields


def test_send_reply_has_no_recipient_field() -> None:
    assert set(SendReply.model_fields) == {"action", "subject", "body"}


def test_link_order_is_the_only_action_with_order_id() -> None:
    models_with_order_id = [
        action_model
        for action_model in ACTION_MODEL_CLASSES
        if "order_id" in action_model.model_fields
    ]

    assert models_with_order_id == [LinkOrder]


def test_all_action_models_forbid_extra_fields() -> None:
    assert all_action_models_forbid_extra_fields()


def test_action_field_names_are_unique_per_model() -> None:
    for action_model in ACTION_MODEL_CLASSES:
        field_names = list(action_model.model_fields)
        assert len(field_names) == len(set(field_names))


def test_propose_refund_model_accepts_unquantised_decimal_for_policy_checks() -> None:
    refund = ProposeRefund.model_validate(
        {"action": "propose_refund", "amount": "12.340"},
    )

    assert refund.amount.as_tuple().exponent == -3


def test_parser_rejects_unquantised_illegal_decimal_scale() -> None:
    result = parse('{"action": "propose_refund", "amount": "12.340"}')

    assert isinstance(result, ParseFailure)
