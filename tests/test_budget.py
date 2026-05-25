"""Budget boundary conditions."""
import pytest

from src.budget import (
    AnthropicBudget,
    WallclockExceeded,
    WallclockGuard,
    WhisperBudget,
    actual_cost_from_usage,
    estimate_sonnet_cost,
)


def test_whisper_budget_can_afford_within_limit():
    b = WhisperBudget(minutes_remaining=60.0, assumed_rtf=8.0)
    # 60 min wallclock at 8x RTF buys 480 audio-minutes
    assert b.can_afford(audio_minutes=400)


def test_whisper_budget_cannot_afford_overflow():
    b = WhisperBudget(minutes_remaining=10.0, assumed_rtf=8.0)
    # 10 min wallclock at 8x = 80 audio-min; can't fit a 200-min audio
    assert not b.can_afford(audio_minutes=200)


def test_whisper_budget_spend_clamps_at_zero():
    b = WhisperBudget(minutes_remaining=5.0, assumed_rtf=8.0)
    b.spend(10.0)
    assert b.minutes_remaining == 0.0
    assert b.minutes_used == 10.0


def test_anthropic_budget_blocks_overflow():
    b = AnthropicBudget(warn_usd=5.0, stop_usd=10.0)
    assert b.can_afford(5.0)
    b.spend(8.0)
    assert not b.can_afford(5.0)
    assert b.can_afford(1.5)


def test_anthropic_budget_warns_once(caplog):
    import logging
    b = AnthropicBudget(warn_usd=2.0, stop_usd=10.0)
    with caplog.at_level(logging.WARNING):
        b.spend(3.0)
        b.spend(1.0)
    warnings = [r for r in caplog.records if "warn threshold" in r.message]
    assert len(warnings) == 1


def test_sonnet_cost_estimate():
    # 100K input tokens + 1500 output tokens
    cost = estimate_sonnet_cost(100_000, 1500)
    # $3/M input × 0.1 = $0.30 + $15/M output × 0.0015 = $0.0225 → ~$0.32
    assert 0.30 < cost < 0.35


def test_actual_cost_from_usage_with_caching():
    class Usage:
        input_tokens = 1000
        output_tokens = 500
        cache_creation_input_tokens = 2000
        cache_read_input_tokens = 0
    cost = actual_cost_from_usage(Usage())
    # base: 1000 × $3 / 1M = 0.003
    # write: 2000 × $3 × 1.25 / 1M = 0.0075
    # output: 500 × $15 / 1M = 0.0075
    assert abs(cost - (0.003 + 0.0075 + 0.0075)) < 1e-6


def test_wallclock_guard_raises():
    import time
    g = WallclockGuard(total_minutes=0.001)  # 60 ms
    time.sleep(0.1)
    with pytest.raises(WallclockExceeded):
        g.check()
