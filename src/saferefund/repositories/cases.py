"""Case correlation lookups and verification-token resolution."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from saferefund.domain.events import EventType
from saferefund.domain.payloads import VerificationRequestedPayload
from saferefund.domain.tables import CaseRow, EventRow


@dataclass(frozen=True, slots=True)
class VerificationRequestLookup:
    """A verification token and the case that issued it."""

    case_id: str
    customer_id: str
    event_id: str
    token: str
    expires_at: datetime


async def list_cases_for_customer_ordered(
    session: AsyncSession,
    customer_id: str,
) -> list[CaseRow]:
    """Return every case for a customer in ascending creation order."""
    statement = (
        select(CaseRow)
        .where(CaseRow.customer_id == customer_id)
        .order_by(CaseRow.created_at)
    )
    return list(await session.scalars(statement))


async def find_case_for_customer_by_opening_message_id(
    session: AsyncSession,
    customer_id: str,
    opening_message_id: str,
) -> CaseRow | None:
    """Return the case opened by one message identifier for the owning customer."""
    statement = select(CaseRow).where(
        CaseRow.customer_id == customer_id,
        CaseRow.opening_message_id == opening_message_id,
    )
    case_row = await session.scalar(statement)
    if case_row is None:
        return None
    return case_row


async def find_verification_request_by_token(
    session: AsyncSession,
    token: str,
) -> VerificationRequestLookup | None:
    """Return the issuing case for a verification token when one exists."""
    statement = select(EventRow).where(
        EventRow.type == EventType.VERIFICATION_REQUESTED.value,
        EventRow.payload["token"].as_string() == token,
    )
    event_row = await session.scalar(statement)
    if event_row is None or event_row.case_id is None:
        return None

    verification_payload = VerificationRequestedPayload.model_validate(
        event_row.payload
    )
    return VerificationRequestLookup(
        case_id=event_row.case_id,
        customer_id=event_row.customer_id,
        event_id=event_row.id,
        token=verification_payload.token,
        expires_at=verification_payload.expires_at,
    )
