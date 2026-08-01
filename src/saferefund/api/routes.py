"""HTTP routes that orchestrate gate operations and the agent loop."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from saferefund import clock, ids
from saferefund.agent.gateway import ModelGateway  # noqa: TC001
from saferefund.agent.loop import run_agent_loop
from saferefund.api.dependencies import (  # noqa: TC001
    ModelGatewayDep,
    SessionFactoryDep,
)
from saferefund.api.schemas import (
    CaseEventResponse,
    InboundEmailRequest,
    InboundEmailResponse,
    OperatorActionResponse,
    OperatorApproveRequest,
    OperatorConflictResponse,
    OperatorPendingRefundResponse,
    OperatorPendingResponse,
    OperatorRejectRequest,
    UnknownSenderResponse,
    VerificationConfirmRequest,
    VerificationConfirmResponse,
    VerificationExpiredResponse,
)
from saferefund.domain.enums import Actor, CaseStatus, Channel
from saferefund.domain.events import EventType
from saferefund.domain.payloads import (
    CaseOpenedPayload,
    EmailReceivedPayload,
)
from saferefund.domain.tables import CaseRow
from saferefund.gate.operations import (
    approve_refund,
    confirm_verification,
    expire_due_refunds_for_customer,
    reject_refund,
    send_unknown_sender_reply,
)
from saferefund.gate.outcomes import OperatorResultKind, VerificationResultKind
from saferefund.projections.case import project_case_summary
from saferefund.projections.types import FoldableEvent  # noqa: TC001
from saferefund.repositories.cases import (
    find_case_for_customer_by_opening_message_id,
)
from saferefund.repositories.customers import find_customer_by_email
from saferefund.repositories.events import (
    append_canonical_event,
    load_case_events,
    load_customer_events,
)
from saferefund.repositories.refunds import list_active_pending_refunds

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

inbound_router = APIRouter()
operator_router = APIRouter(prefix="/operator")
verification_router = APIRouter(prefix="/verification")


@inbound_router.post(
    "/inbound-email",
    responses={
        status.HTTP_200_OK: {"model": InboundEmailResponse},
        status.HTTP_202_ACCEPTED: {"model": UnknownSenderResponse},
    },
)
async def post_inbound_email(
    request_body: InboundEmailRequest,
    session_factory: SessionFactoryDep,
    model_gateway: ModelGatewayDep,
) -> JSONResponse:
    """Route inbound email to case creation, deduplication, or unknown-sender reply."""
    async with session_factory.begin() as session:
        customer_row = await find_customer_by_email(
            session,
            request_body.envelope_from,
        )
        if customer_row is None:
            await send_unknown_sender_reply(request_body.envelope_from)
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content=UnknownSenderResponse().model_dump(),
            )

        reopened_case_ids = await expire_due_refunds_for_customer(
            session,
            customer_id=customer_row.id,
        )
        case_id, should_run_loop = await _resolve_inbound_case(
            session,
            customer_id=customer_row.id,
            message_id=request_body.message_id,
            subject=request_body.subject,
            body=request_body.body,
        )
        customer_id = customer_row.id

    resume_case_ids: list[str] = list(reopened_case_ids)
    if should_run_loop and case_id not in reopened_case_ids:
        resume_case_ids.append(case_id)
    await _drain_agent_loop_queue(
        session_factory, tuple(resume_case_ids), model_gateway
    )

    async with session_factory() as session:
        inbound_response = await _build_inbound_email_response(
            session,
            case_id=case_id,
            customer_id=customer_id,
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content=inbound_response.model_dump(),
    )


@operator_router.get("/pending", response_model=OperatorPendingResponse)
async def get_operator_pending(
    session_factory: SessionFactoryDep,
) -> OperatorPendingResponse:
    """List active pending refunds without expiry housekeeping or event writes."""
    now = clock.now()
    async with session_factory() as session:
        pending_refund_rows = await list_active_pending_refunds(session, now)
    return OperatorPendingResponse(
        pending_refunds=[
            OperatorPendingRefundResponse(
                refund_id=pending_refund_row.id,
                case_id=pending_refund_row.case_id,
                order_id=pending_refund_row.order_id,
                amount=pending_refund_row.amount,
                approval_expires_at=pending_refund_row.approval_expires_at,
            )
            for pending_refund_row in pending_refund_rows
            if pending_refund_row.approval_expires_at is not None
        ],
    )


@operator_router.post("/approve", response_model=OperatorActionResponse)
async def post_operator_approve(
    request_body: OperatorApproveRequest,
    session_factory: SessionFactoryDep,
    model_gateway: ModelGatewayDep,
) -> OperatorActionResponse:
    """Approve one pending refund and resume the owning case when successful."""
    async with session_factory.begin() as session:
        outcome = await approve_refund(
            session,
            request_body.refund_id,
            request_body.operator_id,
            session_factory=session_factory,
        )

    if outcome.kind is OperatorResultKind.CONFLICT:
        await _resume_cases_in_order(
            session_factory,
            outcome.reopened_case_ids,
            model_gateway,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=OperatorConflictResponse(
                refund_id=outcome.refund_id,
                refund_status=outcome.refund_status,
            ).model_dump(),
        )

    await _resume_cases_in_order(
        session_factory,
        outcome.reopened_case_ids,
        model_gateway,
        also_resume=outcome.case_id,
    )
    return OperatorActionResponse(
        case_id=outcome.case_id,
        refund_id=outcome.refund_id,
        refund_status=outcome.refund_status,
    )


@operator_router.post("/reject", response_model=OperatorActionResponse)
async def post_operator_reject(
    request_body: OperatorRejectRequest,
    session_factory: SessionFactoryDep,
    model_gateway: ModelGatewayDep,
) -> OperatorActionResponse:
    """Reject one pending refund and resume the owning case when successful."""
    async with session_factory.begin() as session:
        outcome = await reject_refund(
            session,
            request_body.refund_id,
            request_body.operator_id,
            request_body.reason,
        )

    if outcome.kind is OperatorResultKind.CONFLICT:
        await _resume_cases_in_order(
            session_factory,
            outcome.reopened_case_ids,
            model_gateway,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=OperatorConflictResponse(
                refund_id=outcome.refund_id,
                refund_status=outcome.refund_status,
            ).model_dump(),
        )

    await _resume_cases_in_order(
        session_factory,
        outcome.reopened_case_ids,
        model_gateway,
        also_resume=outcome.case_id,
    )
    return OperatorActionResponse(
        case_id=outcome.case_id,
        refund_id=outcome.refund_id,
        refund_status=outcome.refund_status,
    )


@verification_router.post("/confirm", response_model=VerificationConfirmResponse)
async def post_verification_confirm(
    request_body: VerificationConfirmRequest,
    session_factory: SessionFactoryDep,
    model_gateway: ModelGatewayDep,
) -> VerificationConfirmResponse:
    """Confirm a verification token and resume every now-open customer case."""
    async with session_factory.begin() as session:
        outcome = await confirm_verification(session, request_body.token)

    if outcome.kind is VerificationResultKind.NOT_FOUND:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if outcome.kind is VerificationResultKind.EXPIRED:
        if outcome.issuing_case_id is not None:
            await _resume_open_case(
                session_factory,
                outcome.issuing_case_id,
                model_gateway,
            )
        if outcome.customer_id is None or outcome.issuing_case_id is None:
            message = "Expired verification outcome missing resume identifiers."
            raise RuntimeError(message)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=VerificationExpiredResponse(
                customer_id=outcome.customer_id,
                issuing_case_id=outcome.issuing_case_id,
            ).model_dump(),
        )

    if outcome.customer_id is None:
        message = "Verified outcome missing customer identifier."
        raise RuntimeError(message)

    await _drain_agent_loop_queue(session_factory, outcome.open_case_ids, model_gateway)

    return VerificationConfirmResponse(
        customer_id=outcome.customer_id,
        resumed_case_ids=list(outcome.open_case_ids),
    )


async def _resolve_inbound_case(
    session: AsyncSession,
    *,
    customer_id: str,
    message_id: str,
    subject: str,
    body: str,
) -> tuple[str, bool]:
    """Return the case id and whether the agent loop should run for an open case."""
    existing_case = await find_case_for_customer_by_opening_message_id(
        session,
        customer_id,
        message_id,
    )
    if existing_case is not None:
        case_is_open = await _case_is_open(
            session,
            case_id=existing_case.id,
            customer_id=customer_id,
        )
        return existing_case.id, case_is_open

    try:
        async with session.begin_nested():
            new_case_id = await _create_case_for_inbound_email(
                session,
                customer_id=customer_id,
                message_id=message_id,
                subject=subject,
                body=body,
            )
    except IntegrityError:
        raced_case = await find_case_for_customer_by_opening_message_id(
            session,
            customer_id,
            message_id,
        )
        if raced_case is None:
            raise
        case_is_open = await _case_is_open(
            session,
            case_id=raced_case.id,
            customer_id=customer_id,
        )
        return raced_case.id, case_is_open

    return new_case_id, True


async def _create_case_for_inbound_email(
    session: AsyncSession,
    *,
    customer_id: str,
    message_id: str,
    subject: str,
    body: str,
) -> str:
    """Persist one new case and its opening events inside the caller transaction."""
    new_case_id = ids.case_id()
    session.add(
        CaseRow(
            id=new_case_id,
            customer_id=customer_id,
            opening_message_id=message_id,
            created_at=clock.now(),
        )
    )
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=customer_id,
        case_id=new_case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload=CaseOpenedPayload(opening_message_id=message_id),
    )
    await append_canonical_event(
        session,
        event_type=EventType.EMAIL_RECEIVED,
        customer_id=customer_id,
        case_id=new_case_id,
        actor=Actor.CUSTOMER,
        channel=Channel.EMAIL,
        payload=EmailReceivedPayload(
            message_id=message_id,
            subject=subject,
            body=body,
        ),
    )
    return new_case_id


async def _case_may_run_agent_loop(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
) -> bool:
    customer_events = await load_customer_events(session, customer_id)
    customer_event_stream = cast("tuple[FoldableEvent, ...]", tuple(customer_events))
    case_summary = project_case_summary(
        case_id=case_id,
        customer_id=customer_id,
        events=customer_event_stream,
        now=clock.now(),
    )
    return case_summary.status is not CaseStatus.CLOSED


async def _case_is_open(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
) -> bool:
    customer_events = await load_customer_events(session, customer_id)
    customer_event_stream = cast("tuple[FoldableEvent, ...]", tuple(customer_events))
    case_summary = project_case_summary(
        case_id=case_id,
        customer_id=customer_id,
        events=customer_event_stream,
        now=clock.now(),
    )
    return case_summary.status is CaseStatus.OPEN


async def _resume_cases_in_order(
    session_factory: async_sessionmaker[AsyncSession],
    reopened_case_ids: tuple[str, ...],
    model_gateway: ModelGateway,
    *,
    also_resume: str | None = None,
) -> None:
    """Resume each open case once in the sweep's deterministic order."""
    queue_case_ids = list(reopened_case_ids)
    if also_resume is not None and also_resume not in reopened_case_ids:
        queue_case_ids.append(also_resume)
    await _drain_agent_loop_queue(session_factory, tuple(queue_case_ids), model_gateway)


async def _resume_open_case(
    session_factory: async_sessionmaker[AsyncSession],
    case_id: str,
    model_gateway: ModelGateway,
) -> None:
    """Run the agent loop when the target case is still open."""
    await _drain_agent_loop_queue(session_factory, (case_id,), model_gateway)


async def _drain_agent_loop_queue(
    session_factory: async_sessionmaker[AsyncSession],
    initial_case_ids: tuple[str, ...],
    model_gateway: ModelGateway,
) -> None:
    """Drive one case loop at a time; enqueue sweep-returned peer ids after commit."""
    pending_case_ids: list[str] = []
    seen_case_ids: set[str] = set()
    for case_id in initial_case_ids:
        if case_id in seen_case_ids:
            continue
        pending_case_ids.append(case_id)
        seen_case_ids.add(case_id)

    queue_index = 0
    while queue_index < len(pending_case_ids):
        case_id = pending_case_ids[queue_index]
        queue_index += 1

        async with session_factory() as session:
            case_row = await session.get(CaseRow, case_id)
            if case_row is None:
                continue
            if not await _case_may_run_agent_loop(
                session,
                case_id=case_id,
                customer_id=case_row.customer_id,
            ):
                continue

        async with session_factory() as session:
            reopened_peer_ids = await run_agent_loop(
                session,
                case_id,
                model_gateway,
                session_factory=session_factory,
            )
            await session.commit()

        for peer_case_id in reopened_peer_ids:
            if peer_case_id in seen_case_ids:
                continue
            pending_case_ids.append(peer_case_id)
            seen_case_ids.add(peer_case_id)


async def _build_inbound_email_response(
    session: AsyncSession,
    *,
    case_id: str,
    customer_id: str,
) -> InboundEmailResponse:
    case_events = await load_case_events(session, case_id)
    customer_events = await load_customer_events(session, customer_id)
    customer_event_stream = cast("tuple[FoldableEvent, ...]", tuple(customer_events))
    case_summary = project_case_summary(
        case_id=case_id,
        customer_id=customer_id,
        events=customer_event_stream,
        now=clock.now(),
    )
    return InboundEmailResponse(
        case_id=case_id,
        status=case_summary.status,
        events=[
            CaseEventResponse(
                seq=event.seq,
                type=event.event_type.value,
                actor=event.actor.value,
                channel=event.channel.value,
            )
            for event in case_events
        ],
    )


__all__ = ["run_agent_loop"]
