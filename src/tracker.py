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
from .discovery import DiscoveryTotalFailure, discover_all, search_byperson, search_itunes
from .filters import assign_priority_scores, classify
from .models import DigestEntry, Episode
from .render_email import render, render_failure
from .state import (
    add_seen_episodes,
    already_ran_today,
    load_seen_guids,
    mark_ran_today,
)
from .spotify import enrich_with_spotify_urls
from .models import EpisodeCandidate, Summary, SummaryPoint, Transcript
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
    skip_idempotency_guard: bool = False,
    lookback_hours: int | None = None,
) -> int:
    """The cron path. Returns the exit code."""
    if not skip_idempotency_guard and already_ran_today():
        logger.info("already ran today; exiting cleanly")
        return 0

    wallclock_guard = WallclockGuard(settings.budgets.total_wallclock_minutes)
    lookback = lookback_hours or settings.schedule.lookback_hours

    try:
        already_seen = load_seen_guids()
        try:
            candidates = await discover_all(
                watchlist,
                lookback_hours=lookback,
                already_seen=already_seen,
            )
        except DiscoveryTotalFailure as e:
            run_date = datetime.now(timezone.utc).date()
            failure_digest = render_failure(run_date, str(e))
            if dry_run:
                logger.info("dry run; discovery failed and would send failure email")
                print(failure_digest.subject)
                print()
                print(failure_digest.text)
                return 0
            try:
                send_digest(failure_digest, idempotency_key=f"discovery-fail-{run_date.isoformat()}")
            except DigestSendError:
                logger.exception("discovery-failure email failed; state NOT committed")
                return 2
            # NOTE: do NOT mark_ran_today() so the next scheduled run retries.
            return 4

        if not candidates:
            entries: list[DigestEntry] = []
        else:
            episodes = classify(candidates)
            tier_weights = watchlist.match_priority.tier_weights.model_dump()
            assign_priority_scores(episodes, tier_weights)
            await enrich_with_spotify_urls(episodes)
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
            send_digest(rendered, idempotency_key=f"digest-{run_date.isoformat()}")
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


async def run_once_search(
    query: str,
    *,
    settings: Settings,
    limit: int = 1,
    lookback_hours: int | None = None,
) -> int:
    """Discovery-only smoke test: iTunes search → first candidate → full pipeline → email."""
    wallclock_guard = WallclockGuard(settings.budgets.total_wallclock_minutes)
    lookback = lookback_hours or settings.schedule.lookback_hours
    async with httpx.AsyncClient() as client:
        candidates = await search_itunes(
            client, query,
            match_type="named_person",
            lookback_hours=lookback,
            max_results=25,
        )
    if not candidates:
        logger.info("no iTunes hits for %r in the last %dh", query, lookback)
        return 0
    candidates = candidates[:limit]
    logger.info("found %d candidate(s) for %r", len(candidates), query)
    episodes = classify(candidates)
    tier_weights = {"named_person": 50, "company": 10, "specific_podcast": 100}
    assign_priority_scores(episodes, tier_weights)
    if not episodes:
        logger.info("filter pass dropped all candidates")
        return 0
    await enrich_with_spotify_urls(episodes)
    entries = await _run_pipeline(episodes, settings, wallclock_guard=wallclock_guard)
    run_date = datetime.now(timezone.utc).date()
    rendered = render(entries, run_date)
    try:
        # No idempotency key for one-off smoke tests so they're re-runnable.
        send_digest(rendered, idempotency_key=None)
    except DigestSendError:
        logger.exception("digest send failed")
        return 2
    return 0


def _build_preview_entries() -> list[DigestEntry]:
    """Two fake DigestEntries for fast template iteration. No API calls."""
    base_dt = datetime(2026, 5, 22, tzinfo=timezone.utc)

    cand1 = EpisodeCandidate(
        guid="preview-1",
        title="Sam Altman on the GPT-6 launch and OpenAI's path to AGI",
        description="",
        podcast="Dwarkesh Podcast",
        podcast_feed_id=None,
        published_at=base_dt,
        duration_minutes=125.0,
        episode_url="https://www.dwarkesh.com/p/sam-altman",
        audio_url=None,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type="specific_podcast",
        match_query="Dwarkesh Podcast",
        discovered_via="rss",
        spotify_url="https://open.spotify.com/episode/abc123sample",
    )
    summary1 = Summary(
        bullets=[
            SummaryPoint(n=1, point="Altman targeted a Q3 2026 release for GPT-6, citing internal benchmarks that show ~3x improvement over GPT-5 on reasoning tasks."),
            SummaryPoint(n=2, point="OpenAI grew from 1,200 to 2,100 employees in 2025 with most of the hiring concentrated in research and inference engineering."),
            SummaryPoint(n=3, point="\"We're not running out of training data — we're running out of the right kind of training data,\" Altman said when pressed on scaling limits."),
            SummaryPoint(n=4, point="OpenAI signed a $50B compute commitment with CoreWeave running through 2030, framed as a hedge against Microsoft Azure capacity."),
            SummaryPoint(n=5, point="The hosts speculated that the recent leadership departures point to a structural rift over AGI safety timelines, but Altman declined to confirm."),
        ],
        guests=["Sam Altman"],
        guest_role_and_company="CEO of OpenAI",
    )
    ep1 = Episode(candidate=cand1, filter_confidence=1.0, priority_score=105.0)
    entry1 = DigestEntry(
        episode=ep1,
        transcript=Transcript(text="x", source="podcast_namespace"),
        summary=summary1,
    )

    cand2 = EpisodeCandidate(
        guid="preview-2",
        title="Brad Gerstner on AI infrastructure capex and the 2026 funding cycle",
        description="",
        podcast="Invest Like the Best",
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 20, tzinfo=timezone.utc),
        duration_minutes=78.0,
        episode_url="https://joincolossus.com/episodes/brad-gerstner-2026",
        audio_url=None,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type="named_person",
        match_query="Brad Gerstner",
        discovered_via="itunes_search",
        spotify_url="https://open.spotify.com/episode/def456sample",
    )
    summary2 = Summary(
        bullets=[
            SummaryPoint(n=1, point="Gerstner pegged 2026 hyperscaler capex at $510B, up from $385B in 2025, with NVIDIA capturing roughly 60% of incremental dollars."),
            SummaryPoint(n=2, point="Altimeter exited 80% of its Snowflake position in Q1 2026 and reallocated to Anthropic, citing better unit economics in pure model labs."),
            SummaryPoint(n=3, point="\"The picks-and-shovels trade is over. The next leg is application-layer companies with proprietary distribution,\" Gerstner argued."),
            SummaryPoint(n=4, point="Coreweave's stock-based comp is running at 18% of revenue — Gerstner flagged this as the structural risk that gets the most pushback from LPs."),
        ],
        guests=["Brad Gerstner"],
        guest_role_and_company="Founder & CEO of Altimeter Capital",
    )
    ep2 = Episode(candidate=cand2, filter_confidence=0.95, priority_score=58.0)
    entry2 = DigestEntry(
        episode=ep2,
        transcript=Transcript(text="x", source="youtube_captions"),
        summary=summary2,
    )

    return [entry1, entry2]


def run_preview(*, dry_run: bool = False) -> int:
    """Render two fake entries and (optionally) send them. No API calls except Resend."""
    entries = _build_preview_entries()
    run_date = datetime.now(timezone.utc).date()
    rendered = render(entries, run_date)
    if dry_run:
        print(rendered.subject)
        print()
        print(rendered.text)
        return 0
    try:
        send_digest(rendered, idempotency_key=None)
    except DigestSendError:
        logger.exception("preview send failed")
        return 2
    return 0


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="src.tracker")
    sub = parser.add_subparsers(dest="mode", required=True)

    daily = sub.add_parser("daily", help="full daily cron run")
    daily.add_argument("--dry-run", action="store_true")
    daily.add_argument("--skip-idempotency-guard", action="store_true",
                       help="re-run even if today's date is already in last_run.txt")
    daily.add_argument("--lookback-hours", type=int, default=None,
                       help="override settings.schedule.lookback_hours")

    once_search = sub.add_parser("once-search", help="smoke test: search one query, run full pipeline")
    once_search.add_argument("query", type=str)
    once_search.add_argument("--limit", type=int, default=1)
    once_search.add_argument("--lookback-hours", type=int, default=None,
                             help="override settings.schedule.lookback_hours")

    preview = sub.add_parser("preview", help="render and send a 2-episode preview digest with fake data")
    preview.add_argument("--dry-run", action="store_true", help="print to stdout instead of sending")

    args = parser.parse_args()

    settings = load_settings()

    if args.mode == "daily":
        watchlist = load_watchlist()
        return asyncio.run(run_daily(
            watchlist=watchlist,
            settings=settings,
            dry_run=args.dry_run,
            skip_idempotency_guard=args.skip_idempotency_guard,
            lookback_hours=args.lookback_hours,
        ))
    if args.mode == "once-search":
        return asyncio.run(run_once_search(
            args.query,
            settings=settings,
            limit=args.limit,
            lookback_hours=args.lookback_hours,
        ))
    if args.mode == "preview":
        return run_preview(dry_run=args.dry_run)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
