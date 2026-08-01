"""Shared helpers for bounding untrusted text at persistence boundaries."""

from __future__ import annotations

import hashlib

from saferefund import config


def truncate_utf8_bytes(text_bytes: bytes, max_bytes: int) -> str:
    """Return the longest valid UTF-8 prefix within ``max_bytes``."""
    if len(text_bytes) <= max_bytes:
        return text_bytes.decode("utf-8")
    truncated = text_bytes[:max_bytes]
    while truncated:
        try:
            return truncated.decode("utf-8")
        except UnicodeDecodeError:
            truncated = truncated[:-1]
    return ""


def bound_invalid_output_audit(raw_model_output: str) -> tuple[str, int, str]:
    """Return a bounded preview, original UTF-8 byte count, and SHA-256 digest."""
    raw_bytes = raw_model_output.encode("utf-8")
    byte_count = len(raw_bytes)
    digest = hashlib.sha256(raw_bytes).hexdigest()
    preview = truncate_utf8_bytes(raw_bytes, config.INVALID_OUTPUT_PREVIEW_MAX_BYTES)
    return preview, byte_count, digest


__all__ = ["bound_invalid_output_audit", "truncate_utf8_bytes"]
