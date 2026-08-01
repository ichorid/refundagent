from saferefund.domain.enums import (
    Actor,
    CaseOutcome,
    CaseStatus,
    Channel,
    EscalationOrigin,
    OrderStatus,
    RefundStatus,
    VerificationMethod,
)


def test_enum_values_match_the_architecture() -> None:
    assert [member.value for member in Actor] == [
        "customer",
        "agent",
        "operator",
        "system",
    ]
    assert [member.value for member in Channel] == [
        "email",
        "operator_api",
        "verification_api",
        "internal",
    ]
    assert [member.value for member in CaseStatus] == [
        "open",
        "awaiting_approval",
        "awaiting_verification",
        "closed",
    ]
    assert [member.value for member in RefundStatus] == [
        "pending_approval",
        "approved",
        "executed",
        "rejected",
        "expired",
    ]
    assert [member.value for member in EscalationOrigin] == [
        "agent",
        "policy",
        "step_limit",
        "parse_limit",
        "model_failure",
    ]
    assert [member.value for member in OrderStatus] == [
        "delivered",
        "delivered_damaged",
        "shipped",
    ]
    assert [member.value for member in CaseOutcome] == [
        "finished",
        "escalated",
        "step_limit",
        "parse_limit",
        "model_failure",
    ]
    assert [member.value for member in VerificationMethod] == ["seed", "token"]
