"""Verification token confirmation gate operation."""

from sqlalchemy.ext.asyncio import AsyncSession

from saferefund import clock
from saferefund.domain.enums import Actor, Channel, VerificationMethod
from saferefund.domain.events import EventType
from saferefund.domain.payloads import CustomerVerifiedPayload
from saferefund.gate.common import find_open_case_ids_for_customer
from saferefund.gate.outcomes import VerificationOutcome, VerificationResultKind
from saferefund.repositories.cases import find_verification_request_by_token
from saferefund.repositories.events import append_canonical_event


async def confirm_verification(
    session: AsyncSession,
    token: str,
) -> VerificationOutcome:
    """Confirm a verification token and return open case ids routes may resume."""
    verification_lookup = await find_verification_request_by_token(session, token)
    if verification_lookup is None:
        return VerificationOutcome(kind=VerificationResultKind.NOT_FOUND)

    now = clock.now()
    if now >= verification_lookup.expires_at:
        return VerificationOutcome(
            kind=VerificationResultKind.EXPIRED,
            customer_id=verification_lookup.customer_id,
            issuing_case_id=verification_lookup.case_id,
        )

    customer_id = verification_lookup.customer_id
    await append_canonical_event(
        session,
        event_type=EventType.CUSTOMER_VERIFIED,
        customer_id=customer_id,
        case_id=None,
        order_id=None,
        actor=Actor.SYSTEM,
        channel=Channel.VERIFICATION_API,
        payload=CustomerVerifiedPayload(method=VerificationMethod.TOKEN),
    )

    open_case_ids = await find_open_case_ids_for_customer(session, customer_id, now)

    return VerificationOutcome(
        kind=VerificationResultKind.VERIFIED,
        customer_id=customer_id,
        open_case_ids=open_case_ids,
    )
