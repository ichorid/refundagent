"""Mutation tests for the integration adapter-mediation assertion."""

from types import SimpleNamespace

import pytest

from tests.integration import adapter_mediation


def test_only_the_immediate_adapter_caller_can_satisfy_mediation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deeper gate frame must not bless an unmediated immediate caller.

    Mutation that turns red: scan every frame for a gate filename instead of
    checking only the adapter wrapper's immediate caller.
    """
    stack = [
        SimpleNamespace(filename=adapter_mediation.__file__, lineno=1),
        SimpleNamespace(filename=adapter_mediation.__file__, lineno=2),
        SimpleNamespace(filename="src/saferefund/agent/bypass.py", lineno=3),
        SimpleNamespace(filename="src/saferefund/gate/effects.py", lineno=4),
    ]
    monkeypatch.setattr(adapter_mediation.inspect, "stack", lambda: stack)

    with pytest.raises(AssertionError, match="outside gate mediation"):
        adapter_mediation._assert_adapter_call_is_mediated()  # noqa: SLF001
