from decimal import Decimal

import pytest

from saferefund import ids
from saferefund.adapters import (
    mailer,
    payment,
    reset_adapters_for_tests,
    ticketing,
)


@pytest.fixture(autouse=True)
def _reset_adapter_state() -> None:
    reset_adapters_for_tests()
    ids.reset_counter_for_tests()


@pytest.mark.asyncio
async def test_payment_refund_returns_provider_reference() -> None:
    result = await payment.refund(
        idempotency_key="rfnd_1",
        amount=Decimal("24.00"),
    )

    assert result.ok is True
    assert result.provider_ref == "pay_rfnd_1"
    assert payment.calls == [
        payment.RefundCall(idempotency_key="rfnd_1", amount=Decimal("24.00")),
    ]


@pytest.mark.asyncio
async def test_payment_is_idempotent_by_refund_id() -> None:
    first = await payment.refund(
        idempotency_key="rfnd_42",
        amount=Decimal("10.00"),
    )
    second = await payment.refund(
        idempotency_key="rfnd_42",
        amount=Decimal("10.00"),
    )

    assert first.provider_ref == second.provider_ref == "pay_rfnd_42"
    assert len(payment.calls) == 2


@pytest.mark.asyncio
async def test_payment_repeat_records_call_without_new_provider_reference() -> None:
    first = await payment.refund(
        idempotency_key="rfnd_7",
        amount=Decimal("5.00"),
    )
    second = await payment.refund(
        idempotency_key="rfnd_7",
        amount=Decimal("5.00"),
    )

    assert first is not second
    assert first.provider_ref == second.provider_ref
    assert payment.calls[0].idempotency_key == payment.calls[1].idempotency_key


def test_mailer_appends_messages_to_outbox() -> None:
    mailer.send(
        to="sophie@example.com",
        subject="Refund update",
        body="Your refund is on the way.",
    )
    mailer.send(
        to="tom@example.com",
        subject="Verify your account",
        body="token=vtok_1",
    )

    assert mailer.outbox == [
        mailer.OutboxMessage(
            to="sophie@example.com",
            subject="Refund update",
            body="Your refund is on the way.",
        ),
        mailer.OutboxMessage(
            to="tom@example.com",
            subject="Verify your account",
            body="token=vtok_1",
        ),
    ]


def test_ticketing_returns_deterministic_ticket_ids() -> None:
    first_ticket_id = ticketing.escalate(reason="denial loop")
    second_ticket_id = ticketing.escalate(reason="agent requested help")

    assert first_ticket_id == "tkt_1"
    assert second_ticket_id == "tkt_2"
    assert ticketing.escalations == [
        ticketing.EscalationRecord(reason="denial loop", ticket_id="tkt_1"),
        ticketing.EscalationRecord(
            reason="agent requested help",
            ticket_id="tkt_2",
        ),
    ]


@pytest.mark.asyncio
async def test_reset_adapters_for_tests_clears_all_state() -> None:
    await payment.refund(idempotency_key="rfnd_99", amount=Decimal("1.00"))
    mailer.send(to="a@example.com", subject="s", body="b")
    ticketing.escalate(reason="reset check")

    reset_adapters_for_tests()

    assert payment.calls == []
    assert mailer.outbox == []
    assert ticketing.escalations == []
