"""Resource limits applied inside the isolated worker process."""

from __future__ import annotations

import resource
import sys


def apply_worker_limits(
    *,
    cpu_seconds: int,
    memory_bytes: int,
) -> None:
    """Tighten CPU and virtual-memory limits for the worker process."""
    if cpu_seconds > 0:
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
    if memory_bytes > 0:
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))


def verify_isolated_import_path() -> None:
    """Fail fast when the application package is importable in the worker."""
    try:
        import saferefund.adapters  # noqa: F401, PLC0415
    except ImportError:
        return
    print(
        "worker import path must not expose saferefund.adapters",
        file=sys.stderr,
    )
    raise SystemExit(3)
