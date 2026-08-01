"""Proofs for Authorisation threading at gate effect boundaries."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from saferefund.actions.models import (
    Action,
    Escalate,
    GetOrders,
    LinkOrder,
    ProposeRefund,
    SendReply,
)
from saferefund.adapters import mailer, payment, reset_adapters_for_tests, ticketing
from saferefund.domain.enums import Actor, Channel, EscalationOrigin
from saferefund.domain.events import EventType
from saferefund.domain.tables import CaseRow
from saferefund.gate import effects
from saferefund.gate.operations import escalate_case, execute_agent_action
from saferefund.policy.authorisation import Authorisation, AuthorisationError
from saferefund.policy.checks import applicable_obligations
from saferefund.policy.verdicts import ForceEscalate
from saferefund.repositories.events import append_canonical_event, load_case_events
from saferefund.repositories.seed import SOPHIE_CUSTOMER_ID

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"
PRODUCTION_ROOT = SOURCE_ROOT / "saferefund"


def _authorisation_constructor_sites(module_path: Path) -> list[str]:
    module_tree = ast.parse(module_path.read_text(encoding="utf-8"))
    sites: list[str] = []
    for node in ast.walk(module_tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Authorisation":
            sites.append(f"{module_path.relative_to(SOURCE_ROOT)}:{node.lineno}")
    return sites


def test_only_policy_authorise_mints_authorisation_in_production() -> None:
    """Mutation: construct Authorisation outside policy.authorise; this fails."""
    offenders: set[str] = set()
    policy_path = PRODUCTION_ROOT / "policy" / "policy.py"
    for module_path in PRODUCTION_ROOT.rglob("*.py"):
        if module_path.name == "authorisation.py" or module_path == policy_path:
            continue
        offenders.update(_authorisation_constructor_sites(module_path))

    policy_sites = _authorisation_constructor_sites(policy_path)
    assert len(policy_sites) == 1
    assert policy_sites[0].startswith("saferefund/policy/policy.py:")

    assert offenders == set()


async def _open_case(session: AsyncSession, *, case_id: str) -> None:
    session.add(
        CaseRow(
            id=case_id,
            customer_id=SOPHIE_CUSTOMER_ID,
            opening_message_id=f"msg-{case_id}",
            created_at=datetime(2030, 1, 1, tzinfo=UTC),
        )
    )
    await append_canonical_event(
        session,
        event_type=EventType.CASE_OPENED,
        customer_id=SOPHIE_CUSTOMER_ID,
        case_id=case_id,
        actor=Actor.SYSTEM,
        channel=Channel.INTERNAL,
        payload={"opening_message_id": f"msg-{case_id}"},
    )


@pytest.fixture
def authorisation_session_factory(
    seeded_session_factory: async_sessionmaker[AsyncSession],
) -> async_sessionmaker[AsyncSession]:
    reset_adapters_for_tests()
    return seeded_session_factory


async def test_effect_helper_rejects_missing_authorisation(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: drop the authorisation parameter from _allow_send_reply; this fails."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_no_auth")
        case_row = await session.get(CaseRow, "case_no_auth")
        assert case_row is not None
        action = SendReply(action="send_reply", subject="Hi", body="Body")
        with pytest.raises(TypeError):
            await cast("Any", effects._allow_send_reply)(  # noqa: SLF001 — deliberate boundary probe
                session,
                case_row=case_row,
                case_id="case_no_auth",
                action=action,
            )


async def test_mismatched_authorisation_raises_before_adapter(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: skip authorisation.spend identity checks; mailer.send would run."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_mismatch")
        case_row = await session.get(CaseRow, "case_mismatch")
        assert case_row is not None
        bound_action = SendReply(action="send_reply", subject="Hi", body="Body")
        other_action = SendReply(action="send_reply", subject="Hi", body="Body")
        auth = Authorisation(
            case_id="case_mismatch",
            action=bound_action,
            obligations_discharged=applicable_obligations(bound_action),
        )
        with pytest.raises(AuthorisationError, match="does not match"):
            await effects._allow_send_reply(  # noqa: SLF001
                session,
                authorisation=auth,
                case_row=case_row,
                case_id="case_mismatch",
                action=other_action,
            )

    assert len(mailer.outbox) == 0


async def test_agent_escalation_without_authorisation_cannot_reach_ticketing(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: agent escalate_case without capability must not reach ticketing."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_agent_esc")
        with pytest.raises(
            AuthorisationError, match="requires a matching authorisation"
        ):
            await escalate_case(
                session,
                "case_agent_esc",
                origin=EscalationOrigin.AGENT,
            )

    assert len(ticketing.escalations) == 0


async def test_agent_escalation_rejects_non_escalate_authorisation(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: self-comparing spend; a SendReply capability must not ticket."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_wrong_action")
        send_reply = SendReply(action="send_reply", subject="Hi", body="Body")
        auth = Authorisation(
            case_id="case_wrong_action",
            action=send_reply,
            obligations_discharged=applicable_obligations(send_reply),
        )
        with pytest.raises(AuthorisationError, match="must be Escalate"):
            await escalate_case(
                session,
                "case_wrong_action",
                origin=EscalationOrigin.AGENT,
                authorisation=auth,
            )

    assert len(ticketing.escalations) == 0


async def test_agent_escalation_rejects_caller_supplied_reason(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: accept reason= alongside authorisation; ticket reason can diverge."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_agent_reason")
        escalate_action = Escalate(action="escalate", reason="Bound reason")
        auth = Authorisation(
            case_id="case_agent_reason",
            action=escalate_action,
            obligations_discharged=applicable_obligations(escalate_action),
        )
        with pytest.raises(AuthorisationError, match="bound Escalate action"):
            await escalate_case(
                session,
                "case_agent_reason",
                origin=EscalationOrigin.AGENT,
                authorisation=auth,
                reason="Conflicting reason",
            )

    assert len(ticketing.escalations) == 0


async def test_policy_escalation_rejects_conflicting_reason(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: ignore force_escalate.reason and use caller reason for ticketing."""
    force_escalate = ForceEscalate(rule="R_DENIAL_LOOP", reason="Verdict reason")
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_policy_reason")
        with pytest.raises(ValueError, match="ForceEscalate evidence"):
            await escalate_case(
                session,
                "case_policy_reason",
                origin=EscalationOrigin.POLICY,
                force_escalate=force_escalate,
                reason="Conflicting reason",
            )

    assert len(ticketing.escalations) == 0


async def test_agent_escalation_records_bound_reason(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: use caller ``reason=`` instead of the bound ``Escalate.reason``."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_agent_ok")
        escalate_action = Escalate(action="escalate", reason="Need a human")
        auth = Authorisation(
            case_id="case_agent_ok",
            action=escalate_action,
            obligations_discharged=applicable_obligations(escalate_action),
        )
        await escalate_case(
            session,
            "case_agent_ok",
            origin=EscalationOrigin.AGENT,
            authorisation=auth,
        )

    assert len(ticketing.escalations) == 1
    assert ticketing.escalations[0].reason == "Need a human"

    async with authorisation_session_factory() as session:
        events = await load_case_events(session, "case_agent_ok")
        escalated = next(
            event for event in events if event.event_type is EventType.ESCALATED
        )
        assert escalated.payload["reason"] == "Need a human"


async def test_policy_escalation_records_verdict_reason(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: use caller ``reason=`` instead of ``ForceEscalate.reason``."""
    force_escalate = ForceEscalate(rule="R_DENIAL_LOOP", reason="Denial loop exceeded")
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_policy_ok")
        await escalate_case(
            session,
            "case_policy_ok",
            origin=EscalationOrigin.POLICY,
            force_escalate=force_escalate,
        )

    assert len(ticketing.escalations) == 1
    assert ticketing.escalations[0].reason == "Denial loop exceeded"

    async with authorisation_session_factory() as session:
        events = await load_case_events(session, "case_policy_ok")
        escalated = next(
            event for event in events if event.event_type is EventType.ESCALATED
        )
        assert escalated.payload["reason"] == "Denial loop exceeded"


async def test_execute_agent_action_spends_authorisation_once_per_effect(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation: skip ``authorisation.spend`` in ``_allow_send_reply``."""
    original_spend = Authorisation.spend
    spend_calls: list[tuple[str, Action]] = []

    def record_spend(
        authorisation: Authorisation,
        *,
        case_id: str,
        action: Action,
    ) -> None:
        spend_calls.append((case_id, action))
        original_spend(authorisation, case_id=case_id, action=action)

    monkeypatch.setattr(Authorisation, "spend", record_spend)

    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_spend")
        await execute_agent_action(
            session,
            "case_spend",
            SendReply(action="send_reply", subject="Hi", body="There"),
        )

    assert len(spend_calls) == 1
    assert spend_calls[0][0] == "case_spend"
    assert isinstance(spend_calls[0][1], SendReply)
    assert len(mailer.outbox) == 1

    async with authorisation_session_factory() as session:
        events = await load_case_events(session, "case_spend")
        assert events[-1].event_type is EventType.REPLY_SENT


async def test_double_spend_raises_authorisation_error() -> None:
    """Mutation: remove the ``_spent`` guard from ``Authorisation.spend``."""
    action = GetOrders(action="get_orders")
    auth = Authorisation(
        case_id="case_x",
        action=action,
        obligations_discharged=applicable_obligations(action),
    )
    auth.spend(case_id="case_x", action=action)
    with pytest.raises(AuthorisationError, match="already spent"):
        auth.spend(case_id="case_x", action=action)


async def test_denied_proposal_never_reaches_adapter(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: call ``mailer.send`` on policy ``Deny`` in ``execute_agent_action``."""
    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_deny")
        await execute_agent_action(
            session,
            "case_deny",
            ProposeRefund(action="propose_refund", amount=Decimal("10.00")),
        )

    assert len(mailer.outbox) == 0


async def test_require_approval_refund_path_does_not_need_outer_authorisation(
    authorisation_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Mutation: require outer ``Authorisation`` before ``RequireApproval`` dispatch."""
    from saferefund.policy.verdicts import RequireApproval
    from saferefund.repositories.seed import ORD_1003_ID

    async with authorisation_session_factory.begin() as session:
        await _open_case(session, case_id="case_thresh")
        await execute_agent_action(
            session, "case_thresh", GetOrders(action="get_orders")
        )
        await execute_agent_action(
            session,
            "case_thresh",
            LinkOrder(action="link_order", order_id=ORD_1003_ID),
        )
        verdict = await execute_agent_action(
            session,
            "case_thresh",
            ProposeRefund(action="propose_refund", amount=Decimal("600.00")),
        )

    assert isinstance(verdict, RequireApproval)
    assert len(payment.calls) == 0
