"""Inbound deduplication must be scoped to the sending customer.

`message_id` is chosen by the sender. Correlating on it alone lets any known
customer address another customer's case: read its event log, resume its agent
loop, and drive it to closure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import UniqueConstraint
from sqlalchemy.sql.schema import Table

from saferefund.agent.gateway import ModelGateway  # noqa: TC001
from saferefund.domain.enums import CaseStatus
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow
from saferefund.projections.case import project_case_summary
from saferefund.repositories.events import load_case_events, load_customer_events
from saferefund.repositories.seed import (
    SOPHIE_CUSTOMER_ID,
    SOPHIE_EMAIL,
    TOM_CUSTOMER_ID,
    TOM_EMAIL,
)
from tests.conftest import FIXED_TEST_NOW
from tests.invariants.scenario import event_types, open_case_row, post_inbound_email
from tests.support.model_gateway import scripted_gateway

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

SHARED_MESSAGE_ID = "msg-chosen-by-the-sender"


def _finishing_model() -> ModelGateway:
    return scripted_gateway(['{"action": "finish", "summary": "handled"}'])


async def test_reused_message_id_from_another_sender_opens_its_own_case(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A second sender reusing a message id must not be handed the first case."""
    sophie_response = await post_inbound_email(
        api_session_factory,
        _finishing_model(),
        envelope_from=SOPHIE_EMAIL,
        message_id=SHARED_MESSAGE_ID,
    )
    assert sophie_response.status_code == 200
    sophie_case_id = sophie_response.json()["case_id"]

    tom_response = await post_inbound_email(
        api_session_factory,
        _finishing_model(),
        envelope_from=TOM_EMAIL,
        message_id=SHARED_MESSAGE_ID,
    )
    assert tom_response.status_code == 200
    tom_case_id = tom_response.json()["case_id"]

    assert tom_case_id != sophie_case_id

    async with api_session_factory() as session:
        tom_case_row = await session.get(CaseRow, tom_case_id)
        assert tom_case_row is not None
        assert tom_case_row.customer_id == TOM_CUSTOMER_ID


async def test_reused_message_id_does_not_disclose_another_customers_events(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The response to the second sender must describe only that sender's case."""
    await post_inbound_email(
        api_session_factory,
        _finishing_model(),
        envelope_from=SOPHIE_EMAIL,
        message_id=SHARED_MESSAGE_ID,
    )

    tom_response = await post_inbound_email(
        api_session_factory,
        _finishing_model(),
        envelope_from=TOM_EMAIL,
        message_id=SHARED_MESSAGE_ID,
    )

    tom_case_id = tom_response.json()["case_id"]
    async with api_session_factory() as session:
        tom_case_events = await load_case_events(session, tom_case_id)

    disclosed_event_ids = {event["seq"] for event in tom_response.json()["events"]}
    assert disclosed_event_ids == {event.seq for event in tom_case_events}

    async with api_session_factory() as session:
        sophie_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)
    sophie_case_ids = {
        event.case_id for event in sophie_events if event.case_id is not None
    }
    assert tom_case_id not in sophie_case_ids


async def test_reused_message_id_cannot_resume_another_customers_open_case(
    api_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A foreign sender must not be able to run the agent loop on an open case."""
    sophie_case_id = "case_sophie_open"
    async with api_session_factory.begin() as session:
        await open_case_row(
            session,
            case_id=sophie_case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id=SHARED_MESSAGE_ID,
        )

    await post_inbound_email(
        api_session_factory,
        _finishing_model(),
        envelope_from=TOM_EMAIL,
        message_id=SHARED_MESSAGE_ID,
    )

    async with api_session_factory() as session:
        sophie_case_events = await load_case_events(session, sophie_case_id)
        sophie_customer_events = await load_customer_events(session, SOPHIE_CUSTOMER_ID)

    assert event_types(sophie_case_events) == [EventType.CASE_OPENED]

    sophie_case_summary = project_case_summary(
        case_id=sophie_case_id,
        customer_id=SOPHIE_CUSTOMER_ID,
        events=sophie_customer_events,
        now=FIXED_TEST_NOW,
    )
    assert sophie_case_summary.status is CaseStatus.OPEN


async def test_case_lookup_repository_is_customer_scoped() -> None:
    """Correlation lookup must require the resolved customer as an argument."""
    from saferefund.repositories import cases as cases_repository

    lookup = getattr(
        cases_repository,
        "find_case_for_customer_by_opening_message_id",
        None,
    )
    assert lookup is not None, (
        "repositories.cases must expose a customer-scoped correlation lookup"
    )
    assert not hasattr(cases_repository, "find_case_by_opening_message_id"), (
        "the globally scoped correlation lookup must be removed, not kept alongside"
    )


def test_cases_table_is_uniquely_keyed_by_customer_and_message_id() -> None:
    """Uniqueness must be per customer, so a reused message id is not a collision."""
    cases_table = CaseRow.__table__
    assert isinstance(cases_table, Table)
    opening_message_id_column = cases_table.c.opening_message_id

    assert not opening_message_id_column.unique, (
        "opening_message_id must not be globally unique; it is sender-controlled"
    )

    composite_unique_keys = {
        frozenset(column.name for column in constraint.columns)
        for constraint in cases_table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert frozenset({"customer_id", "opening_message_id"}) in composite_unique_keys
