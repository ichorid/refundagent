from datetime import timedelta

from saferefund.domain.enums import (
    Actor,
    CaseStatus,
    Channel,
    EscalationOrigin,
    RefundStatus,
)
from saferefund.domain.events import EventType
from saferefund.projections.case import project_case_summary
from tests.unit.projection_helpers import (
    BASE_TIME,
    customer_verified_event,
    fold_event,
    verification_requested_event,
)


def test_case_starts_open_with_zero_counters() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_tom",
        events=[],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.OPEN
    assert summary.step_count == 0
    assert summary.consecutive_denials == 0
    assert summary.consecutive_invalid_outputs == 0
    assert summary.linked_order_id is None
    assert summary.orders_listed is False
    assert summary.last_refund_status is None
    assert summary.reply_sent_after_last_refund is False


def test_agent_events_increment_step_count() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ORDERS_LISTED,
                actor=Actor.AGENT,
                case_id="case_1",
                payload={"order_ids": ["ORD-1001"]},
            ),
            fold_event(
                seq=2,
                event_type=EventType.ORDER_LINKED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"order_id": "ORD-1001"},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 2
    assert summary.orders_listed is True
    assert summary.linked_order_id == "ORD-1001"


def test_denied_action_increments_step_count() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ACTION_DENIED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "action": "get_orders",
                    "rule": "R_VERIFIED",
                    "agent_reason": "ignored",
                    "customer_reason": "ignored",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 1
    assert summary.consecutive_denials == 1


def test_invalid_output_increments_step_count() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.INVALID_OUTPUT,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "preview": "bad",
                    "byte_count": 3,
                    "sha256": "0" * 64,
                    "error": "parse",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 1
    assert summary.consecutive_invalid_outputs == 1


def test_refund_approval_required_does_not_increment_step_count() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1003",
                payload={"refund_id": "rfnd_1", "amount": "780.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_APPROVAL_REQUIRED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1003",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "780.00",
                    "rule": "R_THRESHOLD",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 1


def test_policy_escalation_counts_one_agent_step() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ESCALATED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "reason": "denial loop",
                    "origin": EscalationOrigin.POLICY.value,
                    "ticket_id": "tkt_1",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.CASE_CLOSED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={"outcome": "escalated", "summary": None},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 1
    assert summary.status is CaseStatus.CLOSED


def test_finished_case_counts_one_agent_step() -> None:
    """Mutation: stop classifying ``case_closed{finished}`` as an agent outcome."""
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.CASE_CLOSED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={"outcome": "finished", "summary": "done"},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 1
    assert summary.status is CaseStatus.CLOSED


def test_policy_escalation_preserves_denial_streak_and_counts_fourth_attempt() -> None:
    """Mutation: treat policy escalation as a successful outcome that resets denials."""
    denied_payload = {
        "action": "finish",
        "rule": "R_TEST_FINISH_DENIED",
        "agent_reason": "ignored",
        "customer_reason": "ignored",
    }
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            *[
                fold_event(
                    seq=seq,
                    event_type=EventType.ACTION_DENIED,
                    actor=Actor.SYSTEM,
                    case_id="case_1",
                    payload=denied_payload,
                )
                for seq in range(1, 4)
            ],
            fold_event(
                seq=4,
                event_type=EventType.ESCALATED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "reason": "denial loop",
                    "origin": EscalationOrigin.POLICY.value,
                    "ticket_id": "tkt_1",
                },
            ),
            fold_event(
                seq=5,
                event_type=EventType.CASE_CLOSED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={"outcome": "escalated", "summary": None},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 4
    assert summary.consecutive_denials == 3
    assert summary.status is CaseStatus.CLOSED


def test_step_limit_escalation_does_not_increment_step_count() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ESCALATED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "reason": "step limit",
                    "origin": EscalationOrigin.STEP_LIMIT.value,
                    "ticket_id": "tkt_1",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.CASE_CLOSED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={"outcome": "step_limit", "summary": None},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.step_count == 0


def test_denial_increments_and_agent_action_resets_consecutive_denials() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ACTION_DENIED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "action": "propose_refund",
                    "rule": "R_NO_LINKED_ORDER",
                    "agent_reason": "ignored",
                    "customer_reason": "ignored",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.ACTION_DENIED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "action": "propose_refund",
                    "rule": "R_NO_LINKED_ORDER",
                    "agent_reason": "ignored",
                    "customer_reason": "ignored",
                },
            ),
            fold_event(
                seq=3,
                event_type=EventType.ORDERS_LISTED,
                actor=Actor.AGENT,
                case_id="case_1",
                payload={"order_ids": ["ORD-1001"]},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.consecutive_denials == 0
    assert summary.step_count == 3


def test_invalid_output_reset_sequence_yields_two_consecutive_invalid_outputs() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.INVALID_OUTPUT,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "preview": "bad",
                    "byte_count": 3,
                    "sha256": "0" * 64,
                    "error": "parse",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.ACTION_DENIED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "action": "get_orders",
                    "rule": "R_VERIFIED",
                    "agent_reason": "ignored",
                    "customer_reason": "ignored",
                },
            ),
            fold_event(
                seq=3,
                event_type=EventType.INVALID_OUTPUT,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "preview": "bad",
                    "byte_count": 3,
                    "sha256": "0" * 64,
                    "error": "parse",
                },
            ),
            fold_event(
                seq=4,
                event_type=EventType.INVALID_OUTPUT,
                actor=Actor.SYSTEM,
                case_id="case_1",
                payload={
                    "preview": "bad",
                    "byte_count": 3,
                    "sha256": "0" * 64,
                    "error": "parse",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.consecutive_invalid_outputs == 2


def test_cross_case_isolation_for_one_customer() -> None:
    events = [
        fold_event(
            seq=1,
            event_type=EventType.ORDERS_LISTED,
            actor=Actor.AGENT,
            case_id="case_a",
            payload={"order_ids": ["ORD-1001"]},
        ),
        fold_event(
            seq=2,
            event_type=EventType.ACTION_DENIED,
            actor=Actor.SYSTEM,
            case_id="case_a",
            payload={
                "action": "propose_refund",
                "rule": "R_NO_LINKED_ORDER",
                "agent_reason": "ignored",
                "customer_reason": "ignored",
            },
        ),
        fold_event(
            seq=3,
            event_type=EventType.ORDER_LINKED,
            actor=Actor.AGENT,
            case_id="case_b",
            order_id="ORD-1002",
            payload={"order_id": "ORD-1002"},
        ),
    ]

    case_a = project_case_summary(
        case_id="case_a",
        customer_id="cust_sophie",
        events=events,
        now=BASE_TIME,
    )
    case_b = project_case_summary(
        case_id="case_b",
        customer_id="cust_sophie",
        events=events,
        now=BASE_TIME,
    )

    assert case_a.orders_listed is True
    assert case_a.linked_order_id is None
    assert case_a.consecutive_denials == 1
    assert case_b.orders_listed is False
    assert case_b.linked_order_id == "ORD-1002"
    assert case_b.consecutive_denials == 0


def test_awaiting_approval_from_approval_required_until_resolution() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1003",
                payload={"refund_id": "rfnd_1", "amount": "780.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_APPROVAL_REQUIRED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1003",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "780.00",
                    "rule": "R_THRESHOLD",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.AWAITING_APPROVAL
    assert summary.last_refund_status is RefundStatus.PENDING_APPROVAL


def test_refund_rejected_clears_pending_and_returns_open() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REFUND_APPROVAL_REQUIRED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1003",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "780.00",
                    "rule": "R_THRESHOLD",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_REJECTED,
                actor=Actor.OPERATOR,
                case_id="case_1",
                order_id="ORD-1003",
                channel=Channel.OPERATOR_API,
                payload={
                    "refund_id": "rfnd_1",
                    "operator_id": "op_1",
                    "reason": "ignored",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.OPEN
    assert summary.last_refund_status is RefundStatus.REJECTED


def test_refund_lifecycle_status_transitions() -> None:
    executed_summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REFUND_PROPOSED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1", "amount": "249.00"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_AUTO_APPROVED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"refund_id": "rfnd_1"},
            ),
            fold_event(
                seq=3,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "249.00",
                    "provider_ref": "pay_rfnd_1",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert executed_summary.last_refund_status is RefundStatus.EXECUTED


def test_reply_sent_after_last_refund_when_sequence_is_later() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "249.00",
                    "provider_ref": "pay_rfnd_1",
                },
            ),
            fold_event(
                seq=2,
                event_type=EventType.REPLY_SENT,
                actor=Actor.AGENT,
                case_id="case_1",
                payload={"subject": "ignored", "body": "ignored"},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.reply_sent_after_last_refund is True


def test_reply_before_refund_lifecycle_leaves_flag_false() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.REPLY_SENT,
                actor=Actor.AGENT,
                case_id="case_1",
                payload={"subject": "ignored", "body": "ignored"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_EXECUTED,
                actor=Actor.SYSTEM,
                case_id="case_1",
                order_id="ORD-1001",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "249.00",
                    "provider_ref": "pay_rfnd_1",
                },
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.reply_sent_after_last_refund is False


def test_case_closed_takes_status_precedence_over_suspensions() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_tom",
        events=[
            verification_requested_event(
                seq=1,
                case_id="case_1",
                customer_id="cust_tom",
                expires_at=BASE_TIME + timedelta(hours=1),
            ),
            fold_event(
                seq=2,
                event_type=EventType.REFUND_APPROVAL_REQUIRED,
                actor=Actor.SYSTEM,
                customer_id="cust_tom",
                case_id="case_1",
                order_id="ORD-2001",
                payload={
                    "refund_id": "rfnd_1",
                    "amount": "60.00",
                    "rule": "R_THRESHOLD",
                },
            ),
            fold_event(
                seq=3,
                event_type=EventType.CASE_CLOSED,
                actor=Actor.SYSTEM,
                customer_id="cust_tom",
                case_id="case_1",
                payload={"outcome": "finished", "summary": "ignored"},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.CLOSED


def test_awaiting_verification_when_token_valid_and_customer_unverified() -> None:
    expires_at = BASE_TIME + timedelta(minutes=15)
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_tom",
        events=[
            verification_requested_event(
                seq=1,
                case_id="case_1",
                expires_at=expires_at,
            )
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.AWAITING_VERIFICATION


def test_expired_verification_returns_case_to_open() -> None:
    expires_at = BASE_TIME - timedelta(minutes=1)
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_tom",
        events=[
            verification_requested_event(
                seq=1,
                case_id="case_1",
                expires_at=expires_at,
            )
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.OPEN


def test_customer_verified_event_unblocks_case_from_awaiting_verification() -> None:
    expires_at = BASE_TIME + timedelta(minutes=15)
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_tom",
        events=[
            verification_requested_event(
                seq=1,
                case_id="case_1",
                expires_at=expires_at,
            ),
            customer_verified_event(seq=2, customer_id="cust_tom"),
        ],
        now=BASE_TIME,
    )

    assert summary.status is CaseStatus.OPEN


def test_relinking_updates_linked_order_id() -> None:
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ORDER_LINKED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1001",
                payload={"order_id": "ORD-1001"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.ORDER_LINKED,
                actor=Actor.AGENT,
                case_id="case_1",
                order_id="ORD-1002",
                payload={"order_id": "ORD-1002"},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.linked_order_id == "ORD-1002"


def test_case_projection_ignores_customer_mismatched_case_events() -> None:
    """Imported malformed history must not contaminate case control state."""
    summary = project_case_summary(
        case_id="case_1",
        customer_id="cust_sophie",
        events=[
            fold_event(
                seq=1,
                event_type=EventType.ORDER_LINKED,
                actor=Actor.AGENT,
                customer_id="cust_tom",
                case_id="case_1",
                order_id="ORD-2001",
                payload={"order_id": "ORD-2001"},
            ),
            fold_event(
                seq=2,
                event_type=EventType.ORDERS_LISTED,
                actor=Actor.AGENT,
                case_id="case_1",
                payload={"order_ids": ["ORD-1001"]},
            ),
        ],
        now=BASE_TIME,
    )

    assert summary.linked_order_id is None
    assert summary.orders_listed is True
    assert summary.step_count == 1
