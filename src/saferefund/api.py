"""Five synchronous FastAPI endpoints over the gate and agent loop."""

from __future__ import annotations

from collections.abc import Generator
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from saferefund import agent, db
from saferefund.agent import HeuristicModel, Model
from saferefund.models import AuditEvent, Case, CaseStatus, OperatorResult
from saferefund.service import (
    approve_refund,
    confirm_verification,
    handle_inbound_email,
    list_pending_refunds,
    reject_refund,
)


class _Req(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Inbound(_Req):
    envelope_from: str = Field(min_length=1)
    message_id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    body: str = Field(min_length=1)


class _OperatorAction(_Req):
    refund_id: str = Field(min_length=1)
    operator_id: str = Field(min_length=1)


class _OperatorReject(_OperatorAction):
    reason: str = Field(min_length=1)


class _Verify(_Req):
    token: str = Field(min_length=1)


def _db_session() -> Generator[Session]:
    """Yield one database session with commit-on-success semantics."""
    session = db.SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


Db = Annotated[Session, Depends(_db_session)]


def _model(request: Request) -> Model:
    return getattr(request.app.state, "model", None) or HeuristicModel()


def _audit_rows(session: Session, case_id: str) -> list[dict[str, Any]]:
    return [
        {
            "id": event.id,
            "type": event.type,
            "detail": event.detail,
            "created_at": event.created_at.isoformat(),
        }
        for event in session.scalars(
            select(AuditEvent)
            .where(AuditEvent.case_id == case_id)
            .order_by(AuditEvent.id)
        ).all()
    ]


def _resume(session: Session, case_ids: tuple[str, ...], model: Model) -> None:
    seen: set[str] = set()
    for case_id in case_ids:
        if case_id in seen:
            continue
        seen.add(case_id)
        case = session.get(Case, case_id)
        if case is not None and case.status == CaseStatus.OPEN.value:
            agent.run_agent_loop(session, case_id, model)


def _resume_ids(
    *, reopened: tuple[str, ...], primary: str | None = None
) -> tuple[str, ...]:
    ordered = list(reopened)
    if primary is not None and primary not in reopened:
        ordered.append(primary)
    return tuple(ordered)


def _operator_done(
    session: Session, request: Request, outcome: OperatorResult
) -> dict[str, str]:
    model = _model(request)
    if outcome.conflict:
        _resume(session, outcome.reopened_case_ids, model)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "refund_id": outcome.refund_id,
                "refund_status": outcome.refund_status,
            },
        )
    _resume(
        session,
        _resume_ids(reopened=outcome.reopened_case_ids, primary=outcome.case_id),
        model,
    )
    return {
        "case_id": outcome.case_id,
        "refund_id": outcome.refund_id,
        "refund_status": outcome.refund_status,
    }


def mount_routes(app: FastAPI) -> None:
    """Attach the five endpoints to an application instance."""
    router = APIRouter()

    @router.post("/inbound-email")
    def post_inbound_email(
        request: Request, body: _Inbound, session: Db
    ) -> JSONResponse:
        inbound = handle_inbound_email(
            session,
            envelope_from=body.envelope_from,
            message_id=body.message_id,
            subject=body.subject,
            body=body.body,
        )
        if inbound.unknown_sender:
            return JSONResponse(
                status_code=status.HTTP_202_ACCEPTED,
                content={"status": "unknown_sender"},
            )
        if inbound.case_id is None:
            raise RuntimeError("Inbound email missing case id for known sender.")
        case = session.get(Case, inbound.case_id)
        if case is None:
            raise LookupError(f"Case not found: {inbound.case_id}")
        model = _model(request)
        resume_ids = _resume_ids(
            reopened=inbound.reopened_case_ids,
            primary=inbound.case_id if case.status == CaseStatus.OPEN.value else None,
        )
        if resume_ids:
            _resume(session, resume_ids, model)
        session.refresh(case)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "case_id": case.id,
                "status": case.status,
                "audit_trail": _audit_rows(session, case.id),
            },
        )

    @router.get("/operator/pending")
    def get_operator_pending(session: Db) -> dict[str, list[dict[str, str]]]:
        return {
            "pending_refunds": [
                {
                    "refund_id": row.id,
                    "case_id": row.case_id,
                    "order_id": row.order_id,
                    "amount": str(row.amount),
                    "approval_expires_at": row.approval_expires_at.isoformat(),
                }
                for row in list_pending_refunds(session)
                if row.approval_expires_at is not None
            ]
        }

    @router.post("/operator/approve")
    def post_operator_approve(
        request: Request, body: _OperatorAction, session: Db
    ) -> dict[str, str]:
        return _operator_done(
            session,
            request,
            approve_refund(
                session, refund_id=body.refund_id, operator_id=body.operator_id
            ),
        )

    @router.post("/operator/reject")
    def post_operator_reject(
        request: Request, body: _OperatorReject, session: Db
    ) -> dict[str, str]:
        return _operator_done(
            session,
            request,
            reject_refund(
                session,
                refund_id=body.refund_id,
                operator_id=body.operator_id,
                reason=body.reason,
            ),
        )

    @router.post("/verification/confirm")
    def post_verification_confirm(
        request: Request, body: _Verify, session: Db
    ) -> dict[str, object]:
        model = _model(request)
        outcome = confirm_verification(session, token=body.token)
        if not outcome.found:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        if outcome.expired:
            if outcome.issuing_case_id is not None:
                _resume(session, (outcome.issuing_case_id,), model)
            if outcome.customer_id is None or outcome.issuing_case_id is None:
                raise RuntimeError(
                    "Expired verification outcome missing resume identifiers."
                )
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "customer_id": outcome.customer_id,
                    "issuing_case_id": outcome.issuing_case_id,
                },
            )
        if outcome.customer_id is None:
            raise RuntimeError("Verified outcome missing customer identifier.")
        _resume(session, outcome.open_case_ids, model)
        return {
            "customer_id": outcome.customer_id,
            "resumed_case_ids": list(outcome.open_case_ids),
        }

    app.include_router(router)
