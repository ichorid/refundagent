"""Integration scenarios 14-15: unknown sender and inbound deduplication."""

from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config
from saferefund.adapters import mailer
from saferefund.agent.gateway import ModelGateway
from saferefund.domain.enums import CaseStatus
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow, EventRow
from saferefund.main import create_app
from saferefund.repositories.seed import (
    SOPHIE_EMAIL,
    TOM_EMAIL,
    UNKNOWN_SENDER_EMAIL,
)
from tests.support.model_gateway import scripted_gateway
from tests.unit.seed_helpers import count_seed_rows


def _action_json(action: str, **fields: str) -> str:
    parts = [f'"action": "{action}"']
    parts.extend(f'"{key}": "{value}"' for key, value in fields.items())
    return "{" + ", ".join(parts) + "}"


def _scripted_model(*actions: str) -> ModelGateway:
    return scripted_gateway(actions)


def _finish_script() -> list[str]:
    return [
        _action_json(
            "send_reply",
            subject="Refund update",
            body="Your refund request has been handled.",
        ),
        _action_json("finish", summary="Idempotent opening message handled."),
    ]


async def test_unknown_sender(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 14: unknown senders receive one canned reply and create no events."""
    async with api_session_factory() as session:
        _, _, seed_event_count = await count_seed_rows(session)

    app = create_app(
        session_factory=api_session_factory, model_gateway=scripted_gateway([])
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": UNKNOWN_SENDER_EMAIL,
                "message_id": "msg-idempotency-unknown",
                "subject": "Hello",
                "body": "Who are you?",
            },
        )

    assert response.status_code == 202
    assert response.json() == {"handled": "unknown_sender"}
    assert len(mailer.outbox) == 1
    unknown_reply = mailer.outbox[0]
    assert unknown_reply.to == UNKNOWN_SENDER_EMAIL
    assert unknown_reply.subject == config.UNKNOWN_SENDER_SUBJECT
    assert unknown_reply.body == config.UNKNOWN_SENDER_BODY

    async with api_session_factory() as session:
        event_count = await session.scalar(select(func.count()).select_from(EventRow))
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        assert event_count == seed_event_count
        assert case_count == 0


async def test_duplicate_message_id_is_idempotent(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Scenario 15: duplicate opening message ids append no second email_received."""
    script = [
        _action_json("get_orders"),
        *_finish_script(),
    ]
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)
    payload = {
        "envelope_from": SOPHIE_EMAIL,
        "message_id": "msg-idempotency-duplicate",
        "subject": "Refund please",
        "body": "First message",
    }

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first_response = await client.post("/inbound-email", json=payload)
        assert first_response.status_code == 200
        first_body: dict[str, Any] = first_response.json()
        assert first_body["status"] == CaseStatus.CLOSED.value
        first_event_count = len(first_body["events"])
        assert [event["type"] for event in first_body["events"]].count(
            EventType.EMAIL_RECEIVED.value,
        ) == 1

        second_response = await client.post(
            "/inbound-email",
            json={
                **payload,
                "subject": "Changed subject",
                "body": "Changed body",
            },
        )

    assert second_response.status_code == 200
    second_body = second_response.json()
    assert second_body["case_id"] == first_body["case_id"]
    assert second_body["status"] == CaseStatus.CLOSED.value
    assert len(second_body["events"]) == first_event_count
    assert [event["type"] for event in second_body["events"]].count(
        EventType.EMAIL_RECEIVED.value,
    ) == 1

    async with api_session_factory() as session:
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        assert case_count == 1


async def test_same_message_id_from_different_senders_opens_separate_cases(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Same message id from two senders is not a dedupe collision across customers."""
    shared_message_id = "msg-idempotency-cross-customer"
    script = [
        _action_json("get_orders"),
        *_finish_script(),
    ]
    app = create_app(
        session_factory=api_session_factory,
        model_gateway=_scripted_model(*script),
    )
    transport = ASGITransport(app=app)

    async with AsyncClient(transport=transport, base_url="http://test") as client:
        sophie_response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": SOPHIE_EMAIL,
                "message_id": shared_message_id,
                "subject": "Refund please",
                "body": "First sender",
            },
        )
        tom_response = await client.post(
            "/inbound-email",
            json={
                "envelope_from": TOM_EMAIL,
                "message_id": shared_message_id,
                "subject": "Refund please",
                "body": "Second sender",
            },
        )

    assert sophie_response.status_code == 200
    assert tom_response.status_code == 200
    sophie_body = sophie_response.json()
    tom_body = tom_response.json()
    assert sophie_body["case_id"] != tom_body["case_id"]
    assert sophie_body["status"] == CaseStatus.CLOSED.value
    assert tom_body["status"] == CaseStatus.CLOSED.value
    assert [event["type"] for event in sophie_body["events"]].count(
        EventType.EMAIL_RECEIVED.value,
    ) == 1
    assert [event["type"] for event in tom_body["events"]].count(
        EventType.EMAIL_RECEIVED.value,
    ) == 1

    async with api_session_factory() as session:
        case_count = await session.scalar(select(func.count()).select_from(CaseRow))
        assert case_count == 2
