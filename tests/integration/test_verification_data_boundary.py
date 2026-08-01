"""Order disclosure requires verification and an authorized orders_listed event."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from httpx import ASGITransport, AsyncClient

from saferefund.adapters import mailer
from saferefund.domain.enums import OrderStatus
from saferefund.main import create_app
from saferefund.repositories.seed import ORD_2001_ID, TOM_EMAIL
from tests.support.model_gateway import gateway_from_prompt_handler

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from saferefund.agent.prompt import Prompt

TOM_ORDER_ITEM = "Electric kettle"
TOM_ORDER_TOTAL = "60.00"
TOM_ORDER_STATUS = OrderStatus.DELIVERED.value

TOM_FORBIDDEN_ORDER_FIELDS: tuple[str, ...] = (
    ORD_2001_ID,
    TOM_ORDER_ITEM,
    TOM_ORDER_TOTAL,
    TOM_ORDER_STATUS,
)


def _forbidden_fields_in_text(text: str) -> tuple[str, ...]:
    return tuple(field for field in TOM_FORBIDDEN_ORDER_FIELDS if field in text)


class OrderLeakingModel:
    """Ignore the advisory menu and echo any disclosed order data in a reply."""

    def __init__(self) -> None:
        self._finished = False
        self.state_forbidden_hits: tuple[str, ...] = ()
        self.text_forbidden_hits: tuple[str, ...] = ()

    async def propose(self, prompt: Prompt) -> str:
        if self._finished:
            return json.dumps({"action": "finish", "summary": "done"})
        self._finished = True

        state_hits: list[str] = []
        leaked_fields: list[str] = []
        for order in prompt.state.orders:
            order_surface = (
                f"{order.id}|{order.item.value}|{order.total}|{order.status.value}"
            )
            for field in TOM_FORBIDDEN_ORDER_FIELDS:
                if field in order_surface:
                    state_hits.append(field)
                    leaked_fields.extend(
                        (
                            f"order={order.id}",
                            f"item={order.item.value}",
                            f"total={order.total}",
                            f"status={order.status.value}",
                        )
                    )
        self.state_forbidden_hits = tuple(dict.fromkeys(state_hits))

        text_hits = list(_forbidden_fields_in_text(prompt.text))
        self.text_forbidden_hits = tuple(text_hits)
        leaked_fields.extend(f"text_{field}={field}" for field in text_hits)

        body = (
            "; ".join(leaked_fields)
            if leaked_fields
            else "no disclosed order data in prompt.state or prompt.text"
        )
        return json.dumps(
            {
                "action": "send_reply",
                "subject": "Account data",
                "body": body,
            }
        )


async def test_unverified_model_cannot_read_or_exfiltrate_order_data(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Verification must precede exposing order details to the model or customer."""
    leaker = OrderLeakingModel()
    model_gateway = gateway_from_prompt_handler(leaker.propose)
    app = create_app(session_factory=api_session_factory, model_gateway=model_gateway)
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": "review-unverified-order-leak",
                "subject": "Hello",
                "body": "Do not reveal account data before verification.",
            },
        )

    assert response.status_code == 200
    assert leaker.state_forbidden_hits == ()
    assert leaker.text_forbidden_hits == ()

    customer_replies = [message for message in mailer.outbox if message.to == TOM_EMAIL]
    assert customer_replies
    assert all("ORD-2001" not in message.body for message in customer_replies)
    for message in customer_replies:
        for field in TOM_FORBIDDEN_ORDER_FIELDS:
            assert field not in message.body
