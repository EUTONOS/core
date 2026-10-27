"""Offline-only Responses prompt-cache policy and cost probes.

Not a transport, Codex setting, model switch, or provider billing assertion.
No imports from an API SDK, network access, storage, timer or side effects.
Caller-provided capabilities/prices must be independently verified at integration.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Mapping, Any


ONE_MILLION = Decimal(1_000_000)
THIRTY_MIN_WRITE = Decimal("1.25")
TWELVE_HOUR_WRITE = Decimal("2.5")
MIN_PREFIX_TOKENS = 1024


def _rate(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("A rate must be a finite nonnegative number")
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("A rate must be a finite nonnegative number") from exc
    if not rate.is_finite() or rate < 0:
        raise ValueError("A rate must be a finite nonnegative number")
    return rate


def _count(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class InputRates:
    """Actual API rates, USD per million input and cached-input tokens.

    Callers must supply current, model-specific rates. Subscription allowance,
    output usage, thinking/reasoning, tax and service fees are outside scope.
    """
    input_per_million: Decimal
    cached_per_million: Decimal

    def __post_init__(self) -> None:
        object.__setattr__(self, "input_per_million", _rate(self.input_per_million))
        object.__setattr__(self, "cached_per_million", _rate(self.cached_per_million))


@dataclass(frozen=True)
class CacheCapability:
    """Explicitly attested by a future *direct Responses API* adapter."""
    direct_responses_api: bool = False
    model_supports_explicit_cache: bool = False
    owner_enabled: bool = False

    @property
    def enabled(self) -> bool:
        return (self.direct_responses_api and self.model_supports_explicit_cache
                and self.owner_enabled)


@dataclass(frozen=True)
class InputUsage:
    input_tokens: int
    cached_tokens: int
    ttl_30m_tokens: int
    ttl_12h_tokens: int

    @property
    def ordinary_tokens(self) -> int:
        return (self.input_tokens - self.cached_tokens
                - self.ttl_30m_tokens - self.ttl_12h_tokens)

    def __post_init__(self) -> None:
        for field in ("input_tokens", "cached_tokens", "ttl_30m_tokens", "ttl_12h_tokens"):
            _count(getattr(self, field), field)
        if self.ordinary_tokens < 0:
            raise ValueError("Cached plus write tokens exceed total input tokens")


def read_input_usage(usage: Mapping[str, Any]) -> InputUsage:
    """Parse a *complete* GPT-5.6+ Responses usage object, fail closed.

    Missing telemetry is unknown, never zero. Cached reads and writes are
    disjoint. Existing EUTONOS native Codex token totals are NOT this shape.
    """
    if not isinstance(usage, Mapping):
        raise ValueError("Responses usage is required")
    details = usage.get("input_tokens_details")
    if not isinstance(details, Mapping):
        raise ValueError("Missing Responses input token detail")
    write = details.get("cache_write_tokens_details")
    if not isinstance(write, Mapping):
        raise ValueError("Missing TTL write breakdown")
    values = InputUsage(
        _count(usage.get("input_tokens"), "input_tokens"),
        _count(details.get("cached_tokens"), "cached_tokens"),
        _count(write.get("ttl_30m_tokens"), "ttl_30m_tokens"),
        _count(write.get("ttl_12h_tokens"), "ttl_12h_tokens"),
    )
    total = _count(details.get("cache_write_tokens"), "cache_write_tokens")
    if total != values.ttl_30m_tokens + values.ttl_12h_tokens:
        raise ValueError("Cache write total and TTL components disagree")
    return values


def input_cost_usd(usage: InputUsage, rates: InputRates) -> Decimal:
    """Input-side charge only. Write rates are TOTAL rates, not surcharges."""
    weighted = (
        Decimal(usage.ordinary_tokens) * rates.input_per_million
        + Decimal(usage.cached_tokens) * rates.cached_per_million
        + Decimal(usage.ttl_30m_tokens) * THIRTY_MIN_WRITE * rates.input_per_million
        + Decimal(usage.ttl_12h_tokens) * TWELVE_HOUR_WRITE * rates.input_per_million
    )
    return weighted / ONE_MILLION


@dataclass(frozen=True)
class Forecast:
    policy: str
    candidates_usd: Mapping[str, Decimal]
    qualification: str


def forecast_reuse(
    prefix_tokens: int,
    windows: int,
    calls_per_window: int,
    rates: InputRates,
    capability: CacheCapability,
    *,
    horizon_hours: Decimal | int | str | None,
) -> Forecast:
    """Compare no write, 30m and 12h for an *identical eligible prefix*.

    Assumptions: each distinct window is separated by an idle gap long
    enough for 30-minute coverage to expire; requests within a window are
    sufficiently close to keep that coverage alive; all windows lie inside
    one <=12h span for a single 12h write. Future hits are hypothetical.
    """
    _count(prefix_tokens, "prefix_tokens")
    if not isinstance(windows, int) or isinstance(windows, bool) or windows < 1:
        raise ValueError("windows must be positive")
    if not isinstance(calls_per_window, int) or isinstance(calls_per_window, bool) or calls_per_window < 1:
        raise ValueError("calls_per_window must be positive")
    hours = None if horizon_hours is None else _rate(horizon_hours)
    total = windows * calls_per_window
    unit_regular = Decimal(prefix_tokens) * rates.input_per_million / ONE_MILLION
    unit_read = Decimal(prefix_tokens) * rates.cached_per_million / ONE_MILLION
    estimates = {"uncached": Decimal(total) * unit_regular}
    if not capability.enabled:
        return Forecast("unsupported", estimates, "No verified, owner-enabled direct Responses adapter")
    if prefix_tokens < MIN_PREFIX_TOKENS:
        return Forecast("uncached", estimates, "Below documented minimum cacheable prefix length")
    estimates["30m"] = Decimal(windows) * THIRTY_MIN_WRITE * unit_regular + Decimal(total - windows) * unit_read
    if hours is not None and hours <= 12:
        estimates["12h"] = TWELVE_HOUR_WRITE * unit_regular + Decimal(total - 1) * unit_read
    # On equal modeled cost, choose the least persistent/machinery-intensive choice.
    chosen = min(estimates, key=lambda name: (estimates[name], {"uncached": 0, "30m": 1, "12h": 2}[name]))
    return Forecast(chosen, estimates, "Scenario only; no observed hit, retention or real billing evidence")


def preview_explicit_request(
    stable: str,
    working: str,
    question: str,
    *,
    stable_ttl: str | None,
    working_ttl: str | None,
    capability: CacheCapability,
) -> dict[str, Any]:
    """Render a transport-free *example fragment*, never submit it.

    Content-block breakpoints mark exact boundaries. Earlier prefix tokens
    are necessarily included in a later working breakpoint's cache.
    """
    if not capability.enabled:
        raise ValueError("Explicit Responses caching is not enabled for this provider/model")
    if any(not isinstance(value, str) or not value for value in (stable, working, question)):
        raise ValueError("All content segments must be nonempty strings")
    if stable_ttl not in (None, "30m", "12h") or working_ttl not in (None, "30m", "12h"):
        raise ValueError("Unsupported TTL")
    if stable_ttl is None and working_ttl is not None:
        raise ValueError("Working prefix cannot be cached without including the earlier stable prefix")
    if stable_ttl == "30m" and working_ttl == "12h":
        raise ValueError("A 12h later breakpoint also rewrites earlier prefix with 12h coverage")
    first: dict[str, Any] = {"type": "input_text", "text": stable}
    second: dict[str, Any] = {"type": "input_text", "text": working}
    if stable_ttl:
        first["prompt_cache_breakpoint"] = {"mode": "explicit", "ttl": stable_ttl}
    if working_ttl:
        second["prompt_cache_breakpoint"] = {"mode": "explicit", "ttl": working_ttl}
    return {
        "prompt_cache_options": {"mode": "explicit", "ttl": "30m"},
        "input": [
            {"role": "developer", "content": [first]},
            {"role": "user", "content": [second]},
            {"role": "user", "content": [{"type": "input_text", "text": question}]},
        ],
    }
