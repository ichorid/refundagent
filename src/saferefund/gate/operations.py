"""Public gate operations that mediate every side effect."""

from typing import assert_never

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund import config
from saferefund.actions.models import Action, Escalate, ProposeRefund
from saferefund.adapters import mailer, ticketing
from saferefund.domain.enums import EscalationOrigin
from saferefund.domain.tables import CaseRow
from saferefund.gate.common import (
    CaseNotFoundError,
    append_action_denied,
    append_escalated_and_case_closed,
    expire_due_refunds_for_customer,
    load_rule_context_for_case,
    session_factory_for,
)
from saferefund.gate.effects import (
    _dispatch_allow_verdict,
    _dispatch_require_approval_verdict,
)
from saferefund.gate.operator import approve_refund, reject_refund
from saferefund.gate.outcomes import OperatorOutcome, VerificationOutcome
from saferefund.gate.refund import execute_propose_refund
from saferefund.gate.verification import confirm_verification
from saferefund.policy.authorisation import Authorisation, AuthorisationError
from saferefund.policy.context import RuleContext
from saferefund.policy.policy import authorise as _policy_authorise
from saferefund.policy.verdicts import (
    Allow,
    Deny,
    ForceEscalate,
    RequireApproval,
    Verdict,
)


def authorise(
    ctx: RuleContext,
    action: Action,
) -> Authorisation | Deny | RequireApproval | ForceEscalate:
    """Gate-bound entry to policy authorisation (patchable in tests)."""
    return _policy_authorise(ctx, action)


async def _finalize_propose_refund_verdict(
    session: AsyncSession,
    case_id: str,
    verdict: Verdict,
) -> Verdict:
    """Escalate when locked refund re-evaluation returns ForceEscalate."""
    if isinstance(verdict, ForceEscalate):
        await escalate_case(
            session,
            case_id,
            origin=EscalationOrigin.POLICY,
            force_escalate=verdict,
        )
    return verdict


async def execute_agent_action(
    session: AsyncSession,
    case_id: str,
    action: Action,
    *,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> Verdict:
    """Evaluate policy and dispatch one parsed agent proposal."""
    refund_session_factory = session_factory or session_factory_for(session)

    if isinstance(action, ProposeRefund):
        case_row = await session.get(CaseRow, case_id)
        if case_row is None:
            raise CaseNotFoundError(case_id)
        verdict = await execute_propose_refund(
            session,
            refund_session_factory,
            case_row=case_row,
            case_id=case_id,
            action=action,
        )
        return await _finalize_propose_refund_verdict(session, case_id, verdict)

    case_row, rule_context = await load_rule_context_for_case(session, case_id)
    verdict_or_auth = authorise(rule_context, action)

    match verdict_or_auth:
        case Deny() as deny_verdict:
            await append_action_denied(
                session,
                customer_id=case_row.customer_id,
                case_id=case_id,
                action=action,
                deny_verdict=deny_verdict,
            )
            return deny_verdict
        case ForceEscalate() as force_escalate_verdict:
            await escalate_case(
                session,
                case_id,
                origin=EscalationOrigin.POLICY,
                force_escalate=force_escalate_verdict,
            )
            return force_escalate_verdict
        case RequireApproval() as require_approval_verdict:
            return await _dispatch_require_approval_verdict(
                session,
                case_row=case_row,
                case_id=case_id,
                action=action,
                require_approval_verdict=require_approval_verdict,
            )
        case Authorisation() as authorisation:
            match action:
                case Escalate():
                    await escalate_case(
                        session,
                        case_id,
                        origin=EscalationOrigin.AGENT,
                        authorisation=authorisation,
                    )
                case _:
                    await _dispatch_allow_verdict(
                        session,
                        authorisation=authorisation,
                        case_row=case_row,
                        case_id=case_id,
                        action=action,
                        rule_context=rule_context,
                    )
            return Allow()


async def escalate_case(  # noqa: PLR0913, C901
    session: AsyncSession,
    case_id: str,
    *,
    origin: EscalationOrigin,
    reason: str | None = None,
    authorisation: Authorisation | None = None,
    force_escalate: ForceEscalate | None = None,
) -> None:
    """Create a ticket, then append escalated and case_closed in one transaction.

    Agent and policy paths derive the ticket/event reason from their evidence so
    a caller cannot supply a conflicting reason string.
    """
    case_row = await session.get(CaseRow, case_id)
    if case_row is None:
        raise CaseNotFoundError(case_id)

    if origin is EscalationOrigin.AGENT:
        if authorisation is None:
            message = "agent escalation requires a matching authorisation"
            raise AuthorisationError(message)
        if force_escalate is not None or reason is not None:
            message = "agent escalation reason must come from the bound Escalate action"
            raise AuthorisationError(message)
        bound_action = authorisation.action
        if not isinstance(bound_action, Escalate):
            message = "authorisation action must be Escalate for agent escalation"
            raise AuthorisationError(message)
        authorisation.spend(case_id=case_id, action=bound_action)
        escalation_reason = bound_action.reason
    elif origin is EscalationOrigin.POLICY:
        if force_escalate is None:
            message = "policy escalation requires ForceEscalate evidence"
            raise ValueError(message)
        if authorisation is not None or reason is not None:
            message = "policy escalation reason must come from ForceEscalate evidence"
            raise ValueError(message)
        escalation_reason = force_escalate.reason
    elif origin in (
        EscalationOrigin.STEP_LIMIT,
        EscalationOrigin.PARSE_LIMIT,
        EscalationOrigin.MODEL_FAILURE,
    ):
        if reason is None:
            message = "trusted system escalation requires reason"
            raise ValueError(message)
        if authorisation is not None or force_escalate is not None:
            message = "trusted system escalation does not accept capability evidence"
            raise ValueError(message)
        escalation_reason = reason
    else:
        assert_never(origin)

    # Effect-first by design (§9.4): ticketing succeeds before durable events.
    # A crash after the adapter returns can leave an orphan ticket or duplicate
    # tickets on retry; no transactional outbox is implemented for the toy.
    ticket_id = ticketing.escalate(reason=escalation_reason)

    await append_escalated_and_case_closed(
        session,
        customer_id=case_row.customer_id,
        case_id=case_id,
        origin=origin,
        reason=escalation_reason,
        ticket_id=ticket_id,
    )


async def send_unknown_sender_reply(email: str) -> None:
    """Send the canned unknown-sender reply without creating a case or events."""
    mailer.send(
        to=email,
        subject=config.UNKNOWN_SENDER_SUBJECT,
        body=config.UNKNOWN_SENDER_BODY,
    )


__all__ = [
    "CaseNotFoundError",
    "OperatorOutcome",
    "VerificationOutcome",
    "approve_refund",
    "confirm_verification",
    "escalate_case",
    "execute_agent_action",
    "expire_due_refunds_for_customer",
    "reject_refund",
    "send_unknown_sender_reply",
]
