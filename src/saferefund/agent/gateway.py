"""Trusted model gateway boundary and transports."""

from __future__ import annotations

import asyncio
import os
import sys
from collections import deque
from collections.abc import Sequence  # noqa: TC003
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from saferefund import config
from saferefund.agent.models import (
    PromptObedientModel,
    ScriptedModelExhaustedError,
    heuristic_action_json,
)
from saferefund.agent.prompt import Prompt  # noqa: TC001
from saferefund.agent.prompt_serialization import (
    deserialize_prompt_bytes,
    serialize_prompt,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_WORKER_ROOT = _PROJECT_ROOT / "worker"
_WORKER_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP")


class ModelGatewayProtocolError(RuntimeError):
    """Raised when a gateway transport violates the byte protocol."""


class ModelGatewayTransport(Protocol):
    """Serialize prompt bytes to response bytes behind the trusted boundary."""

    async def invoke(self, request_bytes: bytes) -> bytes:
        """Return one raw model response payload."""


@dataclass(frozen=True, slots=True)
class _ScriptedTransportState:
    pending_outputs: deque[bytes]


class ScriptedModelGatewayTransport:
    """Trusted transport that returns scripted UTF-8 response bytes."""

    def __init__(self, responses: Sequence[str]) -> None:
        """Store scripted UTF-8 responses in invocation order."""
        self._state = _ScriptedTransportState(
            pending_outputs=deque(response.encode("utf-8") for response in responses),
        )

    async def invoke(self, request_bytes: bytes) -> bytes:
        """Return the next scripted response."""
        del request_bytes
        if not self._state.pending_outputs:
            message = "scripted model outputs exhausted"
            raise ScriptedModelExhaustedError(message)
        return self._state.pending_outputs.popleft()


class HeuristicModelGatewayTransport:
    """Trusted transport that runs the heuristic stub without a subprocess."""

    async def invoke(self, request_bytes: bytes) -> bytes:
        """Deserialize the prompt and return heuristic JSON bytes."""
        prompt = deserialize_prompt_bytes(request_bytes)
        return heuristic_action_json(prompt.state).encode("utf-8")


class PromptObedientModelGatewayTransport:
    """Trusted transport that runs the prompt-obedient adversarial stub."""

    def __init__(self) -> None:
        """Create the trusted prompt-obedient stub transport."""
        self._model = PromptObedientModel()

    async def invoke(self, request_bytes: bytes) -> bytes:
        """Deserialize the prompt and return prompt-obedient JSON bytes."""
        prompt = deserialize_prompt_bytes(request_bytes)
        return (await self._model.propose(prompt)).encode("utf-8")


class SubprocessModelGatewayTransport:
    """Spawn the isolated worker with a minimal import path and resource limits."""

    def __init__(self) -> None:
        """Record the isolated worker launch command."""
        worker_main = _WORKER_ROOT / "saferefund_model_worker" / "__main__.py"
        self._worker_command = (
            sys.executable,
            "-I",
            str(worker_main),
        )

    async def invoke(self, request_bytes: bytes) -> bytes:
        """Run the isolated worker for one framed request."""
        if len(request_bytes) > config.MODEL_WORKER_MAX_REQUEST_BYTES:
            message = (
                "model request exceeds configured byte limit "
                f"({len(request_bytes)} > {config.MODEL_WORKER_MAX_REQUEST_BYTES})"
            )
            raise ModelGatewayProtocolError(message)

        process = await asyncio.create_subprocess_exec(
            *self._worker_command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_worker_environment(),
            cwd=str(_WORKER_ROOT),
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=_frame_message(request_bytes)),
                timeout=config.MODEL_WORKER_WALL_CLOCK_SECONDS,
            )
        except TimeoutError as timeout:
            process.kill()
            await process.wait()
            message = "model worker exceeded wall-clock limit"
            raise ModelGatewayProtocolError(message) from timeout

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()
            message = f"model worker exited with status {process.returncode}"
            if detail:
                message = f"{message}: {detail}"
            raise ModelGatewayProtocolError(message)

        return _read_framed_message(stdout)

    def worker_environment(self) -> dict[str, str]:
        """Expose the worker environment for isolation tests."""
        return _worker_environment()

    @property
    def worker_command(self) -> tuple[str, ...]:
        """Expose the worker launch command for isolation tests."""
        return self._worker_command


class ModelGateway:
    """Trusted application-owned model boundary."""

    __slots__ = ("_transport",)

    def __init__(self, transport: ModelGatewayTransport) -> None:
        """Bind one trusted transport behind the gateway boundary."""
        self._transport = transport

    async def propose(self, prompt: Prompt) -> str:
        """Serialize the prompt, invoke the transport, and return UTF-8 text."""
        request_bytes = serialize_prompt(prompt)
        response_bytes = await self._transport.invoke(request_bytes)
        if len(response_bytes) > config.MODEL_RESPONSE_MAX_BYTES:
            message = (
                "model response exceeds configured byte limit "
                f"({len(response_bytes)} > {config.MODEL_RESPONSE_MAX_BYTES})"
            )
            raise ModelGatewayProtocolError(message)
        return response_bytes.decode("utf-8")

    @classmethod
    def heuristic_subprocess(cls) -> ModelGateway:
        """Return the production gateway backed by the isolated worker."""
        return cls(SubprocessModelGatewayTransport())

    @classmethod
    def heuristic_transport(cls) -> ModelGateway:
        """Return a trusted in-process heuristic transport for deterministic tests."""
        return cls(HeuristicModelGatewayTransport())

    @classmethod
    def from_scripted_responses(cls, responses: Sequence[str]) -> ModelGateway:
        """Return a trusted gateway with scripted serialized responses."""
        return cls(ScriptedModelGatewayTransport(responses))

    @classmethod
    def prompt_obedient_transport(cls) -> ModelGateway:
        """Return the adversarial prompt-obedient trusted transport."""
        return cls(PromptObedientModelGatewayTransport())


def _worker_environment() -> dict[str, str]:
    environment: dict[str, str] = {
        "PYTHONNOUSERSITE": "1",
        "PYTHONPATH": str(_WORKER_ROOT),
        "SAFEREFUND_WORKER_CPU_SECONDS": str(config.MODEL_WORKER_CPU_SECONDS),
        "SAFEREFUND_WORKER_MEMORY_BYTES": str(config.MODEL_WORKER_MEMORY_BYTES),
        "SAFEREFUND_WORKER_REQUEST_MAX_BYTES": str(
            config.MODEL_WORKER_MAX_REQUEST_BYTES
        ),
        "SAFEREFUND_WORKER_RESPONSE_MAX_BYTES": str(config.MODEL_RESPONSE_MAX_BYTES),
    }
    for key in _WORKER_ENV_ALLOWLIST:
        value = os.environ.get(key)
        if value is not None:
            environment[key] = value
    return environment


def _frame_message(payload: bytes) -> bytes:
    return len(payload).to_bytes(4, "big", signed=False) + payload


_PROTOCOL_FRAME_HEADER_BYTES = 4


def _read_framed_message(payload: bytes) -> bytes:
    if len(payload) < _PROTOCOL_FRAME_HEADER_BYTES:
        message = "model worker returned an incomplete protocol frame"
        raise ModelGatewayProtocolError(message)
    length = int.from_bytes(payload[:4], "big", signed=False)
    body = payload[4:]
    if len(body) != length:
        message = (
            "model worker returned a truncated protocol frame "
            f"({len(body)} bytes, expected {length})"
        )
        raise ModelGatewayProtocolError(message)
    return body


__all__ = [
    "HeuristicModelGatewayTransport",
    "ModelGateway",
    "ModelGatewayProtocolError",
    "ModelGatewayTransport",
    "PromptObedientModelGatewayTransport",
    "ScriptedModelGatewayTransport",
    "SubprocessModelGatewayTransport",
]
