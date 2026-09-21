"""Shared cocotb signal helpers.

The generated DUT drives X on its outputs until the first reset completes, so
every read has to cope with "not resolvable yet" instead of raising.
"""

from __future__ import annotations

from typing import Optional


def read_bit(sig) -> Optional[int]:
    """Read a 1-bit signal, returning ``None`` while it is X/Z."""
    value = sig.value
    if hasattr(value, "is_resolvable") and not value.is_resolvable:
        return None
    try:
        return int(value) & 0x1
    except Exception:  # pragma: no cover - defensive
        return None


def read_int(sig) -> Optional[int]:
    """Read a multi-bit signal, returning ``None`` while it is X/Z."""
    value = sig.value
    if hasattr(value, "is_resolvable") and not value.is_resolvable:
        return None
    try:
        return int(value)
    except Exception:  # pragma: no cover - defensive
        return None
