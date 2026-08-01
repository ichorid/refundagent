from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import BaseModel
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from saferefund import config
from saferefund.api.schemas import (
    InboundEmailRequest,
    OperatorApproveRequest,
    OperatorRejectRequest,
    VerificationConfirmRequest,
)
from saferefund.db import (
    create_all,
    create_database_engine,
    create_session_factory,
    dispose_database,
)
from saferefund.domain.enums import OrderStatus, RefundStatus
from saferefund.domain.tables import CaseRow, CustomerRow, OrderRow, RefundRow

_HTTP_UNTRUSTED_STRING_FIELDS: tuple[tuple[type[BaseModel], str, str], ...] = (
    (InboundEmailRequest, "envelope_from", "HTTP_ENVELOPE_FROM_MAX_LENGTH"),
    (InboundEmailRequest, "message_id", "HTTP_MESSAGE_ID_MAX_LENGTH"),
    (InboundEmailRequest, "subject", "HTTP_INBOUND_SUBJECT_MAX_LENGTH"),
    (InboundEmailRequest, "body", "HTTP_INBOUND_BODY_MAX_LENGTH"),
    (OperatorApproveRequest, "refund_id", "HTTP_REFUND_ID_MAX_LENGTH"),
    (OperatorApproveRequest, "operator_id", "HTTP_OPERATOR_ID_MAX_LENGTH"),
    (OperatorRejectRequest, "refund_id", "HTTP_REFUND_ID_MAX_LENGTH"),
    (OperatorRejectRequest, "operator_id", "HTTP_OPERATOR_ID_MAX_LENGTH"),
    (OperatorRejectRequest, "reason", "HTTP_OPERATOR_REASON_MAX_LENGTH"),
    (VerificationConfirmRequest, "token", "HTTP_VERIFICATION_TOKEN_MAX_LENGTH"),
)


def _sync_table_names(sync_session: object) -> list[str]:
    assert isinstance(sync_session, Session)
    bind = sync_session.bind
    assert bind is not None
    return inspect(bind).get_table_names()


@pytest.fixture
async def schema_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_engine = create_database_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'schema.db'}"
    )
    await create_all(database_engine)
    yield create_session_factory(database_engine)
    await dispose_database(database_engine)


async def _insert_customer_order_case(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory.begin() as session:
        session.add_all(
            [
                CustomerRow(id="cust_1", email="customer@example.com", name="Customer"),
                OrderRow(
                    id="ORD-1",
                    customer_id="cust_1",
                    item="Item",
                    total=Decimal("20.00"),
                    status=OrderStatus.DELIVERED,
                ),
                CaseRow(
                    id="case_1",
                    customer_id="cust_1",
                    opening_message_id="message-1",
                    created_at=datetime(2030, 1, 1, tzinfo=UTC),
                ),
            ]
        )


def test_every_untrusted_http_string_has_an_explicit_length_bound() -> None:
    """Schema metadata must carry explicit min/max bounds sourced from config."""
    for model_type, field_name, limit_attr in _HTTP_UNTRUSTED_STRING_FIELDS:
        field_schema = model_type.model_json_schema()["properties"][field_name]
        expected_limit = getattr(config, limit_attr)
        assert field_schema["maxLength"] == expected_limit
        assert field_schema["minLength"] == 1


async def test_schema_contains_the_five_required_tables(
    schema_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with schema_session_factory() as session:
        table_names = await session.run_sync(_sync_table_names)

    assert set(table_names) == {
        "cases",
        "customers",
        "event_sequences",
        "events",
        "orders",
        "refunds",
    }


async def test_duplicate_opening_message_id_fails(
    schema_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_customer_order_case(schema_session_factory)

    with pytest.raises(IntegrityError):
        async with schema_session_factory.begin() as session:
            session.add(
                CaseRow(
                    id="case_2",
                    customer_id="cust_1",
                    opening_message_id="message-1",
                    created_at=datetime(2030, 1, 2, tzinfo=UTC),
                )
            )


async def test_only_one_pending_or_approved_refund_can_be_open(
    schema_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _insert_customer_order_case(schema_session_factory)

    async with schema_session_factory.begin() as session:
        session.add(
            RefundRow(
                id="rfnd_1",
                customer_id="cust_1",
                order_id="ORD-1",
                case_id="case_1",
                amount=Decimal("10.00"),
                status=RefundStatus.EXECUTED,
                approval_expires_at=None,
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    async with schema_session_factory.begin() as session:
        session.add(
            RefundRow(
                id="rfnd_2",
                customer_id="cust_1",
                order_id="ORD-1",
                case_id="case_1",
                amount=Decimal("5.00"),
                status=RefundStatus.PENDING_APPROVAL,
                approval_expires_at=datetime(2030, 1, 1, 1, tzinfo=UTC),
                created_at=datetime(2030, 1, 1, tzinfo=UTC),
            )
        )

    with pytest.raises(IntegrityError):
        async with schema_session_factory.begin() as session:
            session.add(
                RefundRow(
                    id="rfnd_3",
                    customer_id="cust_1",
                    order_id="ORD-1",
                    case_id="case_1",
                    amount=Decimal("4.00"),
                    status=RefundStatus.APPROVED,
                    approval_expires_at=None,
                    created_at=datetime(2030, 1, 1, tzinfo=UTC),
                )
            )
