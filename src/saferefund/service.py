"""The gate: load trusted state, decide, then perform effects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from saferefund import adapters, config, ids, policy
from saferefund import clock as clock_module
from saferefund.actions import (
    Action,
    Escalate,
    Finish,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    RequestVerification,
    SendReply,
)
from saferefund.models import (
    AuditEvent,
    Case,
    CaseOutcome,
    CaseStatus,
    Customer,
    InboundResult,
    OperatorResult,
    Order,
    Refund,
    RefundStatus,
    VerificationResult,
    VerificationToken,
)
from saferefund.policy import Allow, Decision, Deny, PolicyState, RequireApproval

_MONEY_LOCK = RLock()


def audit(
    session: Session,
    *,
    case: Case | None = None,
    type: str,
    **detail: Any,
) -> None:
    """Append one informational audit row in the current transaction."""
    session.add(
        AuditEvent(
            case_id=case.id if case is not None else None,
            customer_id=case.customer_id
            if case is not None
            else detail.pop("customer_id", None),
            type=type,
            detail=detail,
            created_at=clock_module.now(),
        )
    )


def _customer(session: Session, customer_id: str) -> Customer:
    customer = session.get(Customer, customer_id)
    if customer is None:
        raise LookupError(f"Customer not found: {customer_id}")
    return customer


def _policy_state_for(session: Session, case: Case) -> PolicyState:
    customer = _customer(session, case.customer_id)
    linked_order = (
        session.get(Order, case.linked_order_id) if case.linked_order_id else None
    )
    return PolicyState(
        case_status=CaseStatus(case.status),
        consecutive_denials=case.consecutive_denials,
        customer_verified=customer.verified,
        owned_order_ids=frozenset(
            session.scalars(
                select(Order.id).where(Order.customer_id == case.customer_id)
            ).all()
        ),
        linked_order_id=case.linked_order_id,
        linked_order_total=linked_order.total if linked_order else None,
        linked_order_refunded=linked_order.refunded_total if linked_order else None,
        linked_order_has_open_refund=case.linked_order_id is not None
        and session.scalar(
            select(Refund.id).where(
                Refund.order_id == case.linked_order_id,
                Refund.status == RefundStatus.PENDING_APPROVAL.value,
            )
        )
        is not None,
        customer_refunded_total=sum(
            session.scalars(
                select(Refund.amount)
                .join(Case, Refund.case_id == Case.id)
                .where(
                    Case.customer_id == case.customer_id,
                    Refund.status == RefundStatus.EXECUTED.value,
                )
            ).all(),
            start=Decimal("0"),
        ),
        approval_threshold=config.REFUND_APPROVAL_THRESHOLD,
        denial_loop_threshold=config.DENIAL_LOOP_THRESHOLD,
    )


def _escalate_case(
    session: Session, case: Case, *, reason: str, outcome: CaseOutcome
) -> None:
    ticket_id = adapters.ticketing.escalate(reason=reason)
    case.status, case.outcome = CaseStatus.CLOSED.value, outcome.value
    audit(
        session,
        case=case,
        type="escalated",
        reason=reason,
        ticket_id=ticket_id,
        outcome=outcome.value,
    )
    audit(session, case=case, type="case_closed", outcome=outcome.value)


def _park_refund(
    session: Session, case: Case, action: ProposeRefund, decision: RequireApproval
) -> None:
    if case.linked_order_id is None:
        raise RuntimeError("RequireApproval refund requires a linked order.")
    if session.get(Order, case.linked_order_id) is None:
        raise LookupError(f"Order not found: {case.linked_order_id}")
    now = clock_module.now()
    refund_row = Refund(
        id=ids.refund_id(),
        case_id=case.id,
        order_id=case.linked_order_id,
        amount=action.amount,
        status=RefundStatus.PENDING_APPROVAL.value,
        approval_expires_at=now + timedelta(seconds=config.APPROVAL_TTL_SECONDS),
        created_at=now,
    )
    session.add(refund_row)
    case.status = CaseStatus.AWAITING_APPROVAL.value
    audit(
        session,
        case=case,
        type="refund_approval_required",
        refund_id=refund_row.id,
        amount=str(action.amount),
        rule=decision.rule,
        reason=decision.reason,
    )


def _execute_refund_payment(
    session: Session, *, case: Case, order: Order, refund_row: Refund
) -> None:
    payment_result = adapters.payment.refund(
        idempotency_key=refund_row.id, amount=refund_row.amount
    )
    refund_row.status = RefundStatus.EXECUTED.value
    refund_row.provider_ref = payment_result.provider_ref
    order.refunded_total += refund_row.amount
    audit(
        session,
        case=case,
        type="refund_executed",
        refund_id=refund_row.id,
        amount=str(refund_row.amount),
        provider_ref=payment_result.provider_ref,
    )


def _perform(session: Session, case: Case, action: Action) -> None:
    match action:
        case GetOrders():
            case.orders_listed = True
            audit(session, case=case, type="orders_listed")
        case LinkOrder() as link_action:
            case.linked_order_id = link_action.order_id
            audit(
                session, case=case, type="order_linked", order_id=link_action.order_id
            )
        case ProposeRefund() as refund_action:
            if case.linked_order_id is None:
                raise RuntimeError("Refund proposal requires a linked order.")
            order = session.get(Order, case.linked_order_id)
            if order is None:
                raise LookupError(f"Order not found: {case.linked_order_id}")
            refund_row = Refund(
                id=ids.refund_id(),
                case_id=case.id,
                order_id=case.linked_order_id,
                amount=refund_action.amount,
                status=RefundStatus.EXECUTED.value,
                created_at=clock_module.now(),
            )
            session.add(refund_row)
            case.refund_reply_sent = False
            _execute_refund_payment(
                session, case=case, order=order, refund_row=refund_row
            )
        case SendReply() as reply_action:
            adapters.mailer.send(
                to=_customer(session, case.customer_id).email,
                subject=reply_action.subject,
                body=reply_action.body,
            )
            audit(
                session,
                case=case,
                type="reply_sent",
                subject=reply_action.subject,
                body=reply_action.body,
            )
            if (
                session.scalar(select(Refund.id).where(Refund.case_id == case.id))
                is not None
            ):
                case.refund_reply_sent = True
        case RequestVerification():
            customer = _customer(session, case.customer_id)
            token, expires_at = (
                ids.verification_token(),
                clock_module.now() + timedelta(seconds=config.VERIFICATION_TTL_SECONDS),
            )
            session.add(
                VerificationToken(
                    token=token,
                    customer_id=case.customer_id,
                    case_id=case.id,
                    expires_at=expires_at,
                )
            )
            adapters.mailer.send(
                to=customer.email,
                subject=config.VERIFICATION_SUBJECT,
                body=config.VERIFICATION_BODY.format(token=token),
            )
            case.status = CaseStatus.AWAITING_VERIFICATION.value
            audit(
                session,
                case=case,
                type="verification_requested",
                token=token,
                expires_at=expires_at.isoformat(),
            )
        case Escalate() as escalate_action:
            _escalate_case(
                session,
                case,
                reason=escalate_action.reason,
                outcome=CaseOutcome.ESCALATED,
            )
        case Finish() as finish_action:
            case.status, case.outcome = (
                CaseStatus.CLOSED.value,
                CaseOutcome.FINISHED.value,
            )
            audit(
                session,
                case=case,
                type="case_closed",
                outcome=CaseOutcome.FINISHED.value,
                summary=finish_action.summary,
            )


def run_agent_action(session: Session, case: Case, action: Action) -> Decision:
    """Evaluate policy once, then apply the verdict or perform the allowed effect."""
    with _MONEY_LOCK:
        return _run_agent_action_locked(session, case, action)


def _run_agent_action_locked(session: Session, case: Case, action: Action) -> Decision:
    """Apply one already-serialized action after its policy verdict."""
    decision = policy.decide(_policy_state_for(session, case), action)
    case.step_count += 1
    match decision:
        case Deny() as deny:
            case.consecutive_denials += 1
            audit(
                session,
                case=case,
                type="action_denied",
                action=action.action,
                rule=deny.rule,
                agent_reason=deny.agent_reason,
                customer_reason=deny.customer_reason,
            )
        case policy.Escalate() as escalate:
            _escalate_case(
                session, case, reason=escalate.reason, outcome=CaseOutcome.ESCALATED
            )
        case RequireApproval() as require_approval:
            if not isinstance(action, ProposeRefund):
                raise RuntimeError("RequireApproval applies only to propose_refund.")
            _park_refund(session, case, action, require_approval)
        case Allow():
            _perform(session, case, action)
    if not isinstance(decision, Deny):
        case.consecutive_denials = 0
    case.consecutive_invalid_outputs = 0
    session.flush()
    return decision


def expire_due_refunds(session: Session, *, customer_id: str) -> list[str]:
    """Expire overdue pending refunds and return case ids reopened to open."""
    now = clock_module.now()
    reopened: list[str] = []
    for refund_row in session.scalars(
        select(Refund)
        .join(Case, Refund.case_id == Case.id)
        .where(
            Case.customer_id == customer_id,
            Refund.status == RefundStatus.PENDING_APPROVAL.value,
            Refund.approval_expires_at.is_not(None),
            Refund.approval_expires_at <= now,
        )
        .order_by(Refund.case_id, Refund.id)
    ).all():
        refund_row.status = RefundStatus.EXPIRED.value
        case = session.get(Case, refund_row.case_id)
        if case is None:
            continue
        audit(session, case=case, type="refund_expired", refund_id=refund_row.id)
        if case.status == CaseStatus.AWAITING_APPROVAL.value:
            case.status = CaseStatus.OPEN.value
            reopened.append(case.id)
    session.flush()
    return reopened


def list_pending_refunds(session: Session) -> list[Refund]:
    """Return non-expired refunds awaiting operator approval."""
    now = clock_module.now()
    return list(
        session.scalars(
            select(Refund).where(
                Refund.status == RefundStatus.PENDING_APPROVAL.value,
                Refund.approval_expires_at.is_not(None),
                Refund.approval_expires_at > now,
            )
        ).all()
    )


def handle_inbound_email(
    session: Session,
    *,
    envelope_from: str,
    message_id: str,
    subject: str,
    body: str,
) -> InboundResult:
    """Route inbound email to case creation or idempotent lookup."""
    customer = session.scalar(
        select(Customer).where(Customer.email == envelope_from.strip().lower())
    )
    if customer is None:
        adapters.mailer.send(
            to=envelope_from.strip().lower(),
            subject=config.UNKNOWN_SENDER_SUBJECT,
            body=config.UNKNOWN_SENDER_BODY,
        )
        return InboundResult(
            case_id=None,
            status="unknown_sender",
            unknown_sender=True,
            reopened_case_ids=(),
        )

    reopened_case_ids = expire_due_refunds(session, customer_id=customer.id)
    existing = session.scalar(
        select(Case).where(
            Case.customer_id == customer.id, Case.opening_message_id == message_id
        )
    )
    if existing is not None:
        return InboundResult(
            case_id=existing.id,
            status=existing.status,
            reopened_case_ids=tuple(reopened_case_ids),
        )

    try:
        with session.begin_nested():
            case = Case(
                id=ids.case_id(),
                customer_id=customer.id,
                opening_message_id=message_id,
                status=CaseStatus.OPEN.value,
                created_at=clock_module.now(),
            )
            session.add(case)
            session.flush()
            audit(session, case=case, type="case_opened", message_id=message_id)
            audit(
                session,
                case=case,
                type="email_received",
                message_id=message_id,
                subject=subject,
                body=body,
            )
    except IntegrityError:
        raced = session.scalar(
            select(Case).where(
                Case.customer_id == customer.id, Case.opening_message_id == message_id
            )
        )
        if raced is None:
            raise
        return InboundResult(
            case_id=raced.id,
            status=raced.status,
            reopened_case_ids=tuple(reopened_case_ids),
        )

    return InboundResult(
        case_id=case.id, status=case.status, reopened_case_ids=tuple(reopened_case_ids)
    )


def _operator_refund(
    session: Session,
    *,
    refund_id: str,
    operator_id: str,
    action: Literal["approve", "reject"],
    reason: str = "",
) -> OperatorResult:
    with _MONEY_LOCK:
        return _operator_refund_locked(
            session,
            refund_id=refund_id,
            operator_id=operator_id,
            action=action,
            reason=reason,
        )


def _operator_refund_locked(
    session: Session,
    *,
    refund_id: str,
    operator_id: str,
    action: Literal["approve", "reject"],
    reason: str = "",
) -> OperatorResult:
    refund_row = session.get(Refund, refund_id)
    if refund_row is None:
        raise LookupError(f"Refund not found: {refund_id}")
    case = session.get(Case, refund_row.case_id)
    if case is None:
        raise LookupError(f"Case not found: {refund_row.case_id}")
    reopened = expire_due_refunds(session, customer_id=case.customer_id)
    session.refresh(refund_row)
    if refund_row.status != RefundStatus.PENDING_APPROVAL.value:
        return OperatorResult(
            case_id=case.id,
            refund_id=refund_id,
            refund_status=refund_row.status,
            conflict=True,
            reopened_case_ids=tuple(reopened),
        )
    if action == "approve":
        order = session.get(Order, refund_row.order_id)
        if order is None:
            raise LookupError(f"Order not found: {refund_row.order_id}")
        _execute_refund_payment(session, case=case, order=order, refund_row=refund_row)
        audit_type = "refund_approved"
        final_status = RefundStatus.EXECUTED.value
    else:
        refund_row.status = RefundStatus.REJECTED.value
        audit_type = "refund_rejected"
        final_status = RefundStatus.REJECTED.value
    case.status = CaseStatus.OPEN.value
    audit(
        session,
        case=case,
        type=audit_type,
        refund_id=refund_id,
        operator_id=operator_id,
        **({"reason": reason} if action == "reject" else {}),
    )
    session.flush()
    return OperatorResult(
        case_id=case.id,
        refund_id=refund_id,
        refund_status=final_status,
        reopened_case_ids=tuple(reopened),
    )


def approve_refund(
    session: Session, *, refund_id: str, operator_id: str
) -> OperatorResult:
    """Approve a pending refund, execute payment, and reopen the owning case."""
    return _operator_refund(
        session, refund_id=refund_id, operator_id=operator_id, action="approve"
    )


def reject_refund(
    session: Session, *, refund_id: str, operator_id: str, reason: str
) -> OperatorResult:
    """Reject a pending refund and reopen the owning case."""
    return _operator_refund(
        session,
        refund_id=refund_id,
        operator_id=operator_id,
        action="reject",
        reason=reason,
    )


def _as_utc(dt: datetime) -> datetime:
    """Normalize SQLite datetimes for comparison with the UTC clock."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def confirm_verification(session: Session, *, token: str) -> VerificationResult:
    """Confirm a verification token and return open case ids to resume."""
    token_row = session.get(VerificationToken, token)
    if token_row is None:
        return VerificationResult(found=False)
    now = clock_module.now()
    if now >= _as_utc(token_row.expires_at):
        return VerificationResult(
            found=True,
            expired=True,
            customer_id=token_row.customer_id,
            issuing_case_id=token_row.case_id,
        )
    customer = _customer(session, token_row.customer_id)
    customer.verified = True
    session.delete(token_row)
    open_case_ids: list[str] = []
    for case in session.scalars(
        select(Case).where(
            Case.customer_id == token_row.customer_id,
            Case.status.in_(
                (CaseStatus.OPEN.value, CaseStatus.AWAITING_VERIFICATION.value)
            ),
        )
    ).all():
        if case.status == CaseStatus.AWAITING_VERIFICATION.value:
            case.status = CaseStatus.OPEN.value
        open_case_ids.append(case.id)
    audit(
        session, case=None, type="customer_verified", customer_id=token_row.customer_id
    )
    session.flush()
    return VerificationResult(
        found=True,
        customer_id=token_row.customer_id,
        open_case_ids=tuple(dict.fromkeys(open_case_ids)),
    )


def escalate_case_system(
    session: Session, case: Case, *, reason: str, outcome: CaseOutcome
) -> None:
    """Close a case after a trusted loop limit or model failure."""
    _escalate_case(session, case, reason=reason, outcome=outcome)
    session.flush()
