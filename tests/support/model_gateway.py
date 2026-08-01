"""Trusted model gateway fixtures for deterministic tests."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence  # noqa: TC003

from saferefund.agent.gateway import ModelGateway
from saferefund.agent.prompt import Prompt  # noqa: TC001
from saferefund.agent.prompt_serialization import deserialize_prompt_bytes


def scripted_gateway(responses: Sequence[str]) -> ModelGateway:
    """Return a trusted gateway with scripted serialized responses."""
    return ModelGateway.from_scripted_responses(responses)


def heuristic_gateway() -> ModelGateway:
    """Return the trusted in-process heuristic gateway transport."""
    return ModelGateway.heuristic_transport()


def prompt_obedient_gateway() -> ModelGateway:
    """Return the trusted prompt-obedient adversarial gateway transport."""
    return ModelGateway.prompt_obedient_transport()


def gateway_from_prompt_handler(
    handler: Callable[[Prompt], Awaitable[str]],
) -> ModelGateway:
    """Return a trusted gateway that delegates to one async prompt handler."""

    class _HandlerTransport:
        async def invoke(self, request_bytes: bytes) -> bytes:
            prompt = deserialize_prompt_bytes(request_bytes)
            return (await handler(prompt)).encode("utf-8")

    return ModelGateway(_HandlerTransport())


def failing_gateway() -> ModelGateway:
    """Return a trusted gateway whose transport always raises."""

    class _FailingTransport:
        async def invoke(self, request_bytes: bytes) -> bytes:
            del request_bytes
            message = "upstream model call failed"
            raise RuntimeError(message)

    return ModelGateway(_FailingTransport())


def stalled_gateway() -> ModelGateway:
    """Return a trusted gateway whose transport never returns."""

    class _StalledTransport:
        async def invoke(self, request_bytes: bytes) -> bytes:
            del request_bytes
            await asyncio.Event().wait()
            message = "stalled gateway transport must not complete"
            raise AssertionError(message)

    return ModelGateway(_StalledTransport())
