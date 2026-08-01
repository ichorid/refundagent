"""Runtime call-origin checks for adapter effect entry points."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any

MEDIATED_CALLER_SUFFIXES = (
    "gate/effects.py",
    "gate/operations.py",
    "gate/refund.py",
)


def _is_mediated_caller(filename: str) -> bool:
    normalized = filename.replace("\\", "/")
    return any(normalized.endswith(suffix) for suffix in MEDIATED_CALLER_SUFFIXES)


def _assert_adapter_call_is_mediated() -> None:
    stack = inspect.stack()
    immediate_caller = stack[2]
    if _is_mediated_caller(immediate_caller.filename):
        return
    caller_chain = " -> ".join(
        f"{frame.filename}:{frame.lineno}" for frame in stack[1:6]
    )
    message = (
        "adapter effect reached from outside gate mediation modules "
        f"({', '.join(MEDIATED_CALLER_SUFFIXES)}); stack: {caller_chain}"
    )
    raise AssertionError(message)


def wrap_asserting_mediated_caller[CallableT: Callable[..., Any]](
    original: CallableT,
) -> CallableT:
    """Wrap an adapter effect so only gate mediation modules may invoke it."""
    if inspect.iscoroutinefunction(original):

        async def async_wrapped(*args: object, **kwargs: object) -> object:
            _assert_adapter_call_is_mediated()
            return await original(*args, **kwargs)

        return async_wrapped  # type: ignore[return-value]

    def sync_wrapped(*args: object, **kwargs: object) -> object:
        _assert_adapter_call_is_mediated()
        return original(*args, **kwargs)

    return sync_wrapped  # type: ignore[return-value]


__all__ = [
    "inspect",
    "wrap_asserting_mediated_caller",
]
