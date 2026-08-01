"""Entry point for the isolated model worker subprocess."""

from __future__ import annotations

import os
import sys
from pathlib import Path

_WORKER_ROOT = Path(__file__).resolve().parent.parent
_APPLICATION_SRC_ROOT = (_WORKER_ROOT.parent / "src").resolve()


def _isolate_worker_import_path() -> None:
    """Drop application source trees while keeping stdlib and worker imports."""
    worker_root = str(_WORKER_ROOT)
    clean_path: list[str] = []
    for entry in sys.path:
        if not entry:
            clean_path.append(entry)
            continue
        resolved = Path(entry).resolve()
        if (
            resolved == _APPLICATION_SRC_ROOT
            or _APPLICATION_SRC_ROOT in resolved.parents
        ):
            continue
        clean_path.append(entry)
    if worker_root not in clean_path:
        clean_path.insert(0, worker_root)
    sys.path[:] = clean_path


_isolate_worker_import_path()

from saferefund_model_worker.heuristic import propose_heuristic
from saferefund_model_worker.limits import (
    apply_worker_limits,
    verify_isolated_import_path,
)
from saferefund_model_worker.protocol import (
    ProtocolViolationError,
    fail_protocol,
    read_message,
    write_message,
)

_DEFAULT_REQUEST_MAX_BYTES = 512 * 1024
_DEFAULT_RESPONSE_MAX_BYTES = 65_536
_DEFAULT_CPU_SECONDS = 5
_DEFAULT_MEMORY_BYTES = 128 * 1024 * 1024


def main() -> int:
    """Read one framed prompt request and write one framed response."""
    verify_isolated_import_path()
    apply_worker_limits(
        cpu_seconds=int(
            os.environ.get("SAFEREFUND_WORKER_CPU_SECONDS", _DEFAULT_CPU_SECONDS)
        ),
        memory_bytes=int(
            os.environ.get("SAFEREFUND_WORKER_MEMORY_BYTES", _DEFAULT_MEMORY_BYTES),
        ),
    )
    request_max_bytes = int(
        os.environ.get(
            "SAFEREFUND_WORKER_REQUEST_MAX_BYTES", _DEFAULT_REQUEST_MAX_BYTES
        ),
    )
    response_max_bytes = int(
        os.environ.get(
            "SAFEREFUND_WORKER_RESPONSE_MAX_BYTES",
            _DEFAULT_RESPONSE_MAX_BYTES,
        ),
    )
    try:
        request_bytes = read_message(sys.stdin.buffer, max_bytes=request_max_bytes)
        response_bytes = propose_heuristic(request_bytes)
        write_message(
            sys.stdout.buffer,
            response_bytes,
            max_bytes=response_max_bytes,
        )
    except ProtocolViolationError as violation:
        fail_protocol(str(violation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
