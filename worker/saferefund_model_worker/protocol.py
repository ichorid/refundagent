"""Length-prefixed byte framing for the model worker stdin/stdout protocol."""

from __future__ import annotations

import sys
from typing import BinaryIO


class ProtocolViolationError(RuntimeError):
    """Raised when worker framing or payload limits are violated."""


def read_message(stream: BinaryIO, *, max_bytes: int) -> bytes:
    """Read one length-prefixed payload from a binary stream."""
    header = _read_exact(stream, 4)
    length = int.from_bytes(header, "big", signed=False)
    if length > max_bytes:
        message = f"request exceeds configured byte limit ({length} > {max_bytes})"
        raise ProtocolViolationError(message)
    return _read_exact(stream, length)


def write_message(stream: BinaryIO, payload: bytes, *, max_bytes: int) -> None:
    """Write one length-prefixed payload to a binary stream."""
    if len(payload) > max_bytes:
        message = (
            f"response exceeds configured byte limit ({len(payload)} > {max_bytes})"
        )
        raise ProtocolViolationError(message)
    stream.write(len(payload).to_bytes(4, "big", signed=False))
    stream.write(payload)
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            message = "worker protocol stream closed before message completed"
            raise ProtocolViolationError(message)
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def fail_protocol(message: str) -> None:
    """Exit the worker with a protocol violation status."""
    print(message, file=sys.stderr)
    raise SystemExit(2)
