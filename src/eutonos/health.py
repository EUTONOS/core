"""Shared advisory health signals that never authorize or block work."""
from __future__ import annotations

import warnings

PROFILE = "eutonos.health-advisory.v1"


def item(code, observed, threshold, message, *, unit="items"):
    return {"profile": PROFILE, "code": code, "severity": "warning",
            "observed": observed, "threshold": threshold, "unit": unit,
            "message": message, "blocking": False}


def add(target, code, observed, threshold, message, *, unit="items"):
    """Append one deduplicated warning after a glass ceiling is crossed."""
    if observed <= threshold:
        return None
    value = item(code, observed, threshold, message, unit=unit)
    if not any(existing.get("code") == code for existing in target):
        target.append(value)
    return value


def emit(code, observed, threshold, message, *, unit="items"):
    """Surface a warning for validators without a structured result envelope."""
    value = item(code, observed, threshold, message, unit=unit)
    if observed > threshold:
        warnings.warn(message + f" Observed {observed} {unit}; advisory threshold is {threshold}.",
                      RuntimeWarning, stacklevel=2)
        return value
    return None
