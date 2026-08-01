from decimal import Decimal

from saferefund.actions.models import (
    ACTION_MODEL_CLASSES,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from tests.unit.labels_helpers import (
    ACTION_LABELS,
    Label,
    all_action_types_have_label_mapping,
    has_label,
    labels_for_action,
)


def test_every_action_type_has_explicit_label_mapping() -> None:
    assert all_action_types_have_label_mapping()
    assert set(ACTION_LABELS) == set(ACTION_MODEL_CLASSES)


def test_label_mapping_matches_architecture() -> None:
    assert labels_for_action(GetOrders(action="get_orders")) == frozenset(
        {Label.READS_PII}
    )
    assert labels_for_action(
        LinkOrder(action="link_order", order_id="ORD-1001")
    ) == frozenset(
        {Label.READS_PII},
    )
    assert labels_for_action(
        ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
    ) == frozenset({Label.MOVES_MONEY})
    assert labels_for_action(
        SendReply(action="send_reply", subject="Hi", body="There"),
    ) == frozenset({Label.EXTERNAL_COMM})
    assert labels_for_action(
        RequestVerification(action="request_verification"),
    ) == frozenset({Label.EXTERNAL_COMM})
    assert labels_for_action(
        Escalate(action="escalate", reason="help"),
    ) == frozenset({Label.INTERNAL_SIDE_EFFECT})
    assert labels_for_action(Finish(action="finish", summary="done")) == frozenset(
        {Label.TERMINAL},
    )


def test_has_label_reflects_static_classification() -> None:
    verification = RequestVerification(action="request_verification")

    assert has_label(verification, Label.EXTERNAL_COMM)
    assert not has_label(verification, Label.READS_PII)
    assert has_label(
        Escalate(action="escalate", reason="help"), Label.INTERNAL_SIDE_EFFECT
    )
    assert not has_label(
        Escalate(action="escalate", reason="help"), Label.EXTERNAL_COMM
    )
