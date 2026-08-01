"""Synthetic sources for adapter-import scanner mutation guards."""

from __future__ import annotations

from typing import Final

FUNCTION_LOCAL_ALIAS_MAILER_SEND = """
def bypass():
    from saferefund.adapters import mailer as m
    m.send(to="x", subject="x", body="x")
"""

FUNCTION_LOCAL_DIRECT_SEND_IMPORT = """
def bypass():
    from saferefund.adapters.mailer import send
    send(to="x", subject="x", body="x")
"""

SYNTHETIC_FUTURE_MODULE_ADAPTER_IMPORT = """
from saferefund.adapters import mailer

def run() -> None:
    mailer.send(to="attacker@example.com", subject="bypass", body="direct adapter")
"""

DEMO_LOCAL_ALIAS_MAILER_SEND = """
from saferefund.adapters import reset_adapters_for_tests
from saferefund.adapters.mailer import snapshot_outbox

def run() -> None:
    reset_adapters_for_tests()
    from saferefund.adapters import mailer as m
    m.send(to="demo@example.com", subject="bypass", body="not read-only")
    snapshot_outbox()
"""

DEMO_MODULE_LEVEL_SEND_MUTATION = """
from saferefund.adapters import mailer, reset_adapters_for_tests

def run() -> None:
    reset_adapters_for_tests()
    mailer.send(to="demo@example.com", subject="bypass", body="not read-only")
"""

REJECTED_ADAPTER_IMPORT_MUTATIONS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "function_local_alias_mailer_send",
        "saferefund.bypass",
        FUNCTION_LOCAL_ALIAS_MAILER_SEND,
    ),
    (
        "function_local_direct_send_import",
        "saferefund.bypass",
        FUNCTION_LOCAL_DIRECT_SEND_IMPORT,
    ),
    (
        "synthetic_future_module_adapter_import",
        "saferefund.bypass",
        SYNTHETIC_FUTURE_MODULE_ADAPTER_IMPORT,
    ),
    (
        "demo_local_alias_mailer_send",
        "saferefund.demo",
        DEMO_LOCAL_ALIAS_MAILER_SEND,
    ),
    (
        "demo_module_level_send_mutation",
        "saferefund.demo",
        DEMO_MODULE_LEVEL_SEND_MUTATION,
    ),
)
