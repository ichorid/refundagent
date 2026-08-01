"""Agent loop support: parse model output into typed actions."""

from saferefund.agent.gateway import ModelGateway
from saferefund.agent.locks import case_execution_lock, reset_case_locks_for_tests
from saferefund.agent.loop import append_invalid_output, run_agent_loop
from saferefund.agent.models import (
    HeuristicModel,
    Model,
    ScriptedModel,
    ScriptedModelExhaustedError,
)
from saferefund.agent.parsing import ParseFailure, ParseSuccess, parse
from saferefund.agent.prompt import (
    AgentState,
    OrderSeedView,
    OrderView,
    Prompt,
    available_actions,
    build_prompt,
    render_memory,
)

__all__ = [
    "AgentState",
    "HeuristicModel",
    "Model",
    "ModelGateway",
    "OrderSeedView",
    "OrderView",
    "ParseFailure",
    "ParseSuccess",
    "Prompt",
    "ScriptedModel",
    "ScriptedModelExhaustedError",
    "append_invalid_output",
    "available_actions",
    "build_prompt",
    "case_execution_lock",
    "parse",
    "render_memory",
    "reset_case_locks_for_tests",
    "run_agent_loop",
]
