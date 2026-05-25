"""Per-run budget enforcement for Whisper wallclock and Anthropic spend."""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Sonnet 4.6 pricing as of 2026-05 ($/M tokens)
SONNET_INPUT_USD_PER_MTOK = 3.00
SONNET_OUTPUT_USD_PER_MTOK = 15.00
SONNET_CACHE_WRITE_5M_MULT = 1.25
SONNET_CACHE_READ_MULT = 0.10

# Haiku 4.5 pricing as of 2026-05
HAIKU_INPUT_USD_PER_MTOK = 1.00
HAIKU_OUTPUT_USD_PER_MTOK = 5.00


@dataclass
class WhisperBudget:
    minutes_remaining: float
    assumed_rtf: float = 8.0
    minutes_used: float = 0.0

    def can_afford(self, audio_minutes: float) -> bool:
        """Estimate wallclock from audio length using the conservative RTF."""
        estimated_wallclock = audio_minutes / max(self.assumed_rtf, 1.0)
        return self.minutes_remaining >= estimated_wallclock

    def spend(self, wallclock_minutes: float) -> None:
        self.minutes_used += wallclock_minutes
        self.minutes_remaining = max(0.0, self.minutes_remaining - wallclock_minutes)


@dataclass
class AnthropicBudget:
    warn_usd: float
    stop_usd: float
    spent_usd: float = 0.0
    _warned: bool = False

    def can_afford(self, estimated_usd: float) -> bool:
        return (self.spent_usd + estimated_usd) <= self.stop_usd

    def spend(self, actual_usd: float) -> None:
        self.spent_usd += actual_usd
        if self.spent_usd >= self.warn_usd and not self._warned:
            logger.warning(
                "anthropic spend exceeded warn threshold",
                extra={"spent_usd": round(self.spent_usd, 4), "warn_usd": self.warn_usd},
            )
            self._warned = True


def estimate_sonnet_cost(input_tokens: int, expected_output_tokens: int = 1500) -> float:
    """Pre-flight cost estimate for one Sonnet 4.6 summarization call (no caching)."""
    return (
        input_tokens / 1_000_000 * SONNET_INPUT_USD_PER_MTOK
        + expected_output_tokens / 1_000_000 * SONNET_OUTPUT_USD_PER_MTOK
    )


def actual_cost_from_usage(usage) -> float:
    """Compute actual USD spend from an Anthropic Usage object.

    Accounts for prompt-cache write/read line items if present.
    `usage` is the SDK's Usage object on the final Message.
    """
    cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
    cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    input_tokens = getattr(usage, "input_tokens", 0) or 0
    output_tokens = getattr(usage, "output_tokens", 0) or 0
    base = input_tokens * SONNET_INPUT_USD_PER_MTOK / 1_000_000
    write = cache_write * SONNET_INPUT_USD_PER_MTOK * SONNET_CACHE_WRITE_5M_MULT / 1_000_000
    read = cache_read * SONNET_INPUT_USD_PER_MTOK * SONNET_CACHE_READ_MULT / 1_000_000
    out = output_tokens * SONNET_OUTPUT_USD_PER_MTOK / 1_000_000
    return base + write + read + out


class WallclockGuard:
    """Hard kill switch above all other budgets. Raises if total run exceeds limit."""

    def __init__(self, total_minutes: float) -> None:
        self.total_minutes = total_minutes
        self._started = time.monotonic()

    def elapsed_minutes(self) -> float:
        return (time.monotonic() - self._started) / 60

    def remaining_minutes(self) -> float:
        return max(0.0, self.total_minutes - self.elapsed_minutes())

    def check(self) -> None:
        if self.elapsed_minutes() > self.total_minutes:
            raise WallclockExceeded(
                f"run exceeded {self.total_minutes:.1f} min wallclock budget"
            )


class WallclockExceeded(RuntimeError):
    pass
