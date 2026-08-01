"""Integration tests for isolated model worker enforcement."""

from __future__ import annotations

import asyncio
import sys

import pytest

from saferefund import config
from saferefund.agent.gateway import SubprocessModelGatewayTransport
from saferefund.agent.prompt import AgentState, Prompt
from saferefund.agent.prompt_serialization import serialize_prompt


def _minimal_prompt_bytes() -> bytes:
    prompt = Prompt(
        text="",
        state=AgentState(
            verified=False,
            orders=(),
            orders_listed=False,
            linked_order_id=None,
            last_refund_status=None,
            reply_sent_after_last_refund=False,
            menu=(),
        ),
    )
    return serialize_prompt(prompt)


async def test_model_worker_has_no_business_adapter_credentials_or_import_path() -> (
    None
):
    """The real subprocess worker must not import adapters or receive app secrets."""
    transport = SubprocessModelGatewayTransport()
    worker_env = transport.worker_environment()

    assert "DATABASE_URL" not in worker_env
    assert "SAFEREFUND_DATABASE_URL" not in worker_env
    for secret_key in (
        "SQLALCHEMY_DATABASE_URL",
        "PAYMENT_API_KEY",
        "MAILER_API_KEY",
        "TICKETING_API_KEY",
    ):
        assert secret_key not in worker_env

    import_probe = f"""import importlib, sys
sys.path[:] = [{worker_env["PYTHONPATH"]!r}]
try:
    importlib.import_module('saferefund.adapters')
except ModuleNotFoundError:
    raise SystemExit(0)
raise SystemExit(1)
"""
    probe = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        import_probe,
        env=worker_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await probe.communicate()
    assert probe.returncode == 0, stderr.decode("utf-8", errors="replace")

    response_bytes = await transport.invoke(_minimal_prompt_bytes())
    assert response_bytes.startswith(b'{"action"')

    assert config.MODEL_WORKER_CPU_SECONDS > 0
    assert config.MODEL_WORKER_MEMORY_BYTES > 0
    assert config.MODEL_RESPONSE_MAX_BYTES > 0

    worker_command = transport.worker_command
    assert worker_command[1] == "-I"
    assert worker_command[2].endswith("saferefund_model_worker/__main__.py")

    oversize_request = b"x" * (config.MODEL_WORKER_MAX_REQUEST_BYTES + 1)
    with pytest.raises(Exception, match="byte limit"):
        await transport.invoke(oversize_request)
