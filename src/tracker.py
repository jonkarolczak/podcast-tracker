"""Main orchestrator for the podcast tracker pipeline.

Entry points:
  python -m src.tracker --once-search "Sam Altman"     # discover + full pipeline for one query
  python -m src.tracker --once-url <youtube_url>       # skip discovery; run pipeline on one URL
  python -m src.tracker --daily                        # full daily run

The --daily path is what GitHub Actions invokes.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import anthropic
import httpx
from dotenv import load_dotenv

# CRITICAL: install secret redactor BEFORE configuring any logging.
from .log_filters import install_root_filter

install_root_filter()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)

logger = logging.getLogger("tracker")

from .budget import AnthropicBudget, WallclockExceeded, WallclockGuard, WhisperBudget
from .config import Settings, Watchlist, load_settings, load_watchlist
from .delivery import DigestSendError, send_digest
from .discovery import discover_all, search_byperson
from .filters import assign_priority_scores, classify
from .models import DigestEntry, Episode, EpisodeCandidate
from .render_email import render
from .state import (
    add_seen_episodes,
    already_ran_today,
    load_seen_guids,
    mark_ran_today,
)
from .summarize import summarize
from .transcript import fetch_transcript


async def _process_one_episode(
    episode: Episode,
    *,
    whisper_budget: WhisperBudget,
    anthropic_budget: AnthropicBudget,
    settings: Settings,
    async_client: anthropic.AsyncAnthropic,
) -> DigestEntry:
    """Transcript → summarize → return a DigestEntry."""
    try:
        transcript = fetch_transcript(
            episode.candidate,
            whisper_budget=whisper_budget,
            whisper_model=settings.transcript.whisper_model,
            whisper_compute_type=settings.transcript.whisper_compute_type,
            whisper_threads=settings.transcript.whisper_threads,
        )
    except Exception as e:  # noqa: BLE001 — wrap any failure into a digest entry
        logger.exception("transcript fetch crashed for %s", episode.candidate.guid)
        return DigestEntry(episode=episode, transcript=None, summary=None, error=str(e))

    if transcript.source == "unavailable":
        return DigestEntry(episode=episode, transcript=transcript, summary=None)

    try:
        summary = await summarize(
            episode, transcript,
            budget=anthropic_budget,
            client=async_client,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("summarize crashed for %s", episode.candidate.guid)
        return DigestEntry(episode=episode, transcript=transcript, summary=None, error=str(e))

    return DigestEntry(episode=episode, transcript=transcript, summary=summary)


async def _run_pipeline(
    episodes: list[Episode],
    settings: Settings,
    *,
    wallclock_guard: WallclockGuard,
    concurrency_summarize: int = 5,
) -> list[DigestEntry]:
    """Process episodes through transcript + summarize. Yields DigestEntry per episode."""
    whisper_budget = WhisperBudget(
        minutes_remaining=settings.budgets.whisper_wallclock_minutes,
        assumed_rtf=settings.budgets.assumed_whisper_rtf,
    )
    anthropic_budget = AnthropicBudget(
        warn_usd=settings.budgets.anthropic_warn_usd,
        stop_usd=settings.budgets.anthropic_hard_stop_usd,
    )
    sem = asyncio.Semaphore(concurrency_summarize)
    async_client = anthropic.AsyncAnthropic(max_retries=5)

    async def _bound(ep: Episode) -> DigestEntry:
        async with sem:
            wallclock_guard.check()
            return await _process_one_episode(
                ep,
                whisper_budget=whisper_budget,
                anthropic_budget=anthropic_budget,
                settings=settings,
                async_client=async_client,
            )

    # Sort by priority descending so high-value episodes process first
    sorted_eps = sorted(episodes, key=lambda e: e.priority_score, reverse=True)
    return await asyncio.gather(*(_bound(ep) for ep in sorted_eps))


async def run_daily(
    *,
    watchlist: Watchlist,
    settings: Settings,
    dry_run: bool = False,
) -> int:
    """The cron path. Returns the exit code."""
    if already_ran_today():
        logger.info("already ran today; exiting cleanly")
        return 0

    wallclock_guard = WallclockGuard(settings.budgets.total_wallclock_minutes)

    try:
        already_seen = load_seen_guids()
        candidates = await discover_all(
            watchlist,
            lookback_hours=settings.schedule.lookback_hours,
            already_seen=already_seen,
        )
        if not candidates:
            entries: list[DigestEntry] = []
        else:
            episodes = classify(candidates)
            tier_weights = watchlist.match_priority.tier_weights.model_dump()
            assign_priority_scores(episodes, tier_weights)
            entries = await _run_pipeline(episodes, settings, wallclock_guard=wallclock_guard)

        run_date = datetime.now(timezone.utc).date()
        rendered = render(entries, run_date)

        if dry_run:
            logger.info("dry run; not sending")
            print(rendered.subject)
            print()
            print(rendered.text)
            return 0

        if not entries and not settings.email.send_when_empty:
            logger.info("zero matches and send_when_empty=false; skipping send")
            mark_ran_today()
            return 0

        try:
            send_digest(rendered, run_date=run_date)
        except DigestSendError:
            logger.exception("digest send failed; state NOT committed")
            return 2

        # Only after successful send: persist state
        successful_candidates = [
            e.episode.candidate for e in entries if e.summary or e.transcript
        ]
        if successful_candidates:
            add_seen_episodes(successful_candidates)
        mark_ran_today()
        return 0

    except WallclockExceeded:
        logger.error("wallclock budget exceeded; aborting without state commit")
        return 3


async def run_once_search(query: str, *, settings: Settings, limit: int = 1) -> int:
    """Discovery-only smoke test: byperson query → first candidate → full pipeline → email."""
    wallclock_guard = WallclockGuard(settings.budgets.total_wallclock_minutes)
    async with httpx.AsyncClient() as client:
        candidates = await search_byperson(
            client, query,
            match_type="named_person",
            lookback_hours=settings.schedule.lookback_hours,
            max_results=20,
        )
    if not candidates:
        logger.info("no PodcastIndex hits for %r in the last %dh", query, settings.schedule.lookback_hours)
        return 0
    candidates = candidates[:limit]
    logger.info("found %d candidate(s) for %r", len(candidates), query)
    episodes = classify(candidates)
    tier_weights = {"named_person": 50, "company": 10, "specific_podcast": 100}
    assign_priority_scores(episodes, tier_weights)
    if not episodes:
        logger.info("filter pass dropped all candidates")
        return 0
    entries = await _run_pipeline(episodes, settings, wallclock_guard=wallclock_guard)
    run_date = datetime.now(timezone.utc).date()
    rendered = render(entries, run_date)
    try:
        send_digest(rendered, run_date=run_date)
    except DigestSendError:
        logger.exception("digest send failed")
        return 2
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="src.tracker")
    sub = parser.add_subparsers(dest="mode", required=True)

    daily = sub.add_parser("daily", help="full daily cron run")
    daily.add_argument("--dry-run", action="store_true")

    once_search = sub.add_parser("once-search", help="smoke test: search one query, run full pipeline")
    once_search.add_argument("query", type=str)
    once_search.add_argument("--limit", type=int, default=1)

    args = parser.parse_args()

    settings = load_settings()

    if args.mode == "daily":
        watchlist = load_watchlist()
        return asyncio.run(run_daily(watchlist=watchlist, settings=settings, dry_run=args.dry_run))
    if args.mode == "once-search":
        return asyncio.run(run_once_search(args.query, settings=settings, limit=args.limit))
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
