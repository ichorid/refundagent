"""Agent loop step, parse, and model failure limits."""

import json
import time

from saferefund import config
from saferefund.agent import ScriptedModel, run_agent_loop
from saferefund.models import Case, CaseOutcome, CaseStatus
from saferefund.service import handle_inbound_email


class _TimeoutModel:
    def propose(self, prompt: str) -> str:
        time.sleep(0.5)
        return json.dumps({"action": "get_orders"})


class _ExplodingModel:
    def propose(self, prompt: str) -> str:
        raise RuntimeError("model exploded")


def test_step_limit_escalates(seeded_session) -> None:
    """Hitting MAX_AGENT_STEPS closes the case with step_limit outcome."""
    session = seeded_session
    inbound = handle_inbound_email(
        session,
        envelope_from="sophie@example.com",
        message_id="msg-step-limit",
        subject="Hi",
        body="Help",
    )
    session.commit()
    case = session.get(Case, inbound.case_id)
    assert case is not None
    case.step_count = config.MAX_AGENT_STEPS
    session.commit()
    run_agent_loop(
        session, case.id, ScriptedModel([json.dumps({"action": "get_orders"})])
    )
    session.commit()
    session.refresh(case)
    assert case.status == CaseStatus.CLOSED.value
    assert case.outcome == CaseOutcome.STEP_LIMIT.value


def test_parse_limit_escalates(seeded_session) -> None:
    """Repeated invalid outputs close the case with parse_limit outcome."""
    session = seeded_session
    inbound = handle_inbound_email(
        session,
        envelope_from="sophie@example.com",
        message_id="msg-parse-limit",
        subject="Hi",
        body="Help",
    )
    session.commit()
    case = session.get(Case, inbound.case_id)
    assert case is not None
    case.consecutive_invalid_outputs = config.MAX_INVALID_OUTPUTS
    session.commit()
    run_agent_loop(session, case.id, ScriptedModel(["not-json"]))
    session.commit()
    session.refresh(case)
    assert case.status == CaseStatus.CLOSED.value
    assert case.outcome == CaseOutcome.PARSE_LIMIT.value


def test_model_timeout_escalates(seeded_session, monkeypatch) -> None:
    """A hard deadline closes the case without waiting for the model to return."""
    session = seeded_session
    monkeypatch.setattr(config, "MODEL_CALL_TIMEOUT_SECONDS", 0.05)
    inbound = handle_inbound_email(
        session,
        envelope_from="sophie@example.com",
        message_id="msg-timeout",
        subject="Hi",
        body="Help",
    )
    session.commit()
    case = session.get(Case, inbound.case_id)
    assert case is not None
    started = time.monotonic()
    run_agent_loop(session, case.id, _TimeoutModel())
    assert time.monotonic() - started < 0.3
    session.commit()
    session.refresh(case)
    assert case.status == CaseStatus.CLOSED.value
    assert case.outcome == CaseOutcome.MODEL_FAILURE.value


def test_model_exception_escalates(seeded_session) -> None:
    """Model exceptions close the case with model_failure outcome."""
    session = seeded_session
    inbound = handle_inbound_email(
        session,
        envelope_from="sophie@example.com",
        message_id="msg-model-exc",
        subject="Hi",
        body="Help",
    )
    session.commit()
    case = session.get(Case, inbound.case_id)
    assert case is not None
    run_agent_loop(session, case.id, _ExplodingModel())
    session.commit()
    session.refresh(case)
    assert case.status == CaseStatus.CLOSED.value
    assert case.outcome == CaseOutcome.MODEL_FAILURE.value
