"""Mock payment adapter with refund-id idempotency and inspectable call history."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RefundCall:
    """One recorded invocation of the mock payment provider."""

    idempotency_key: str
    amount: Decimal


@dataclass(frozen=True, slots=True)
class RefundResult:
    """Successful refund outcome from the mock provider."""

    ok: bool
    provider_ref: str


class _PaymentState:
    def __init__(self) -> None:
        self.calls: list[RefundCall] = []
        self.provider_ref_by_idempotency_key: dict[str, str] = {}

    def existing_provider_ref(self, idempotency_key: str) -> str | None:
        return self.provider_ref_by_idempotency_key.get(idempotency_key)

    def record_provider_ref(self, idempotency_key: str, provider_ref: str) -> None:
        self.provider_ref_by_idempotency_key[idempotency_key] = provider_ref

    def reset(self) -> None:
        self.calls.clear()
        self.provider_ref_by_idempotency_key.clear()


_payment_state = _PaymentState()

calls = _payment_state.calls


async def refund(*, idempotency_key: str, amount: Decimal) -> RefundResult:
    """Execute a mock refund; repeated keys return the same provider reference."""
    _payment_state.calls.append(
        RefundCall(idempotency_key=idempotency_key, amount=amount),
    )
    existing_provider_ref = _payment_state.existing_provider_ref(idempotency_key)
    if existing_provider_ref is not None:
        return RefundResult(ok=True, provider_ref=existing_provider_ref)

    provider_ref = f"pay_{idempotency_key}"
    _payment_state.record_provider_ref(idempotency_key, provider_ref)
    return RefundResult(ok=True, provider_ref=provider_ref)


def reset_payment_for_tests() -> None:
    """Clear recorded calls and idempotency state between tests."""
    _payment_state.reset()
