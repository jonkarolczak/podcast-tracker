"""Discovery: find new podcast episodes matching the watchlist.

Three surfaces:
  - iTunes Search API for both people AND company aliases (free, no auth,
    actual full-text episode search — PodcastIndex byperson only matches
    feeds that publish <podcast:person> tags, which is far too sparse)
  - Direct RSS feed polling for specific podcasts (via feedparser)
  - PodcastIndex is used for OPTIONAL transcript-URL enrichment (its
    transcripts[] field is the canonical Tier-0 transcript lookup)
"""
from __future__ import annotations

import asyncio
import calendar
import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

import feedparser
import httpx

from .config import Watchlist
from .models import EpisodeCandidate, MatchType

logger = logging.getLogger(__name__)

PODCASTINDEX_BASE = "https://api.podcastindex.org/api/1.0"
ITUNES_SEARCH_BASE = "https://itunes.apple.com/search"
USER_AGENT = "podcast-tracker/0.1 (+https://github.com/jonkarolczak/podcast-tracker)"
DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _podcastindex_headers() -> dict[str, str]:
    """Build the four-header signed-SHA1 auth for PodcastIndex."""
    api_key = os.environ["PODCASTINDEX_API_KEY"]
    api_secret = os.environ["PODCASTINDEX_API_SECRET"]
    ts = str(int(time.time()))
    auth = hashlib.sha1(f"{api_key}{api_secret}{ts}".encode()).hexdigest()
    return {
        "User-Agent": USER_AGENT,
        "X-Auth-Key": api_key,
        "X-Auth-Date": ts,
        "Authorization": auth,
    }


def _parse_transcript_field(item: dict[str, Any]) -> tuple[str | None, str | None]:
    """Pull the first usable transcript URL + MIME type from PodcastIndex's transcripts[] array."""
    transcripts = item.get("transcripts") or []
    preferred_types = ("text/vtt", "application/x-subrip", "text/plain", "text/html")
    by_type: dict[str, dict] = {t.get("type", ""): t for t in transcripts if t.get("url")}
    for mime in preferred_types:
        if mime in by_type:
            return by_type[mime]["url"], mime
    if transcripts:
        first = transcripts[0]
        return first.get("url"), first.get("type")
    return None, None


def _episode_from_podcastindex_item(
    item: dict[str, Any],
    match_type: MatchType,
    match_query: str,
) -> EpisodeCandidate:
    published_at = datetime.fromtimestamp(item.get("datePublished") or 0, tz=timezone.utc)
    duration_seconds = item.get("duration") or 0
    transcript_url, transcript_type = _parse_transcript_field(item)
    return EpisodeCandidate(
        guid=item.get("guid") or str(item.get("id") or ""),
        title=item.get("title") or "",
        description=item.get("description") or "",
        podcast=item.get("feedTitle") or "",
        podcast_feed_id=item.get("feedId"),
        published_at=published_at,
        duration_minutes=duration_seconds / 60.0,
        episode_url=item.get("link") or item.get("enclosureUrl") or "",
        audio_url=item.get("enclosureUrl"),
        youtube_url=None,
        podcast_transcript_url=transcript_url,
        podcast_transcript_type=transcript_type,
        match_type=match_type,
        match_query=match_query,
        discovered_via="podcastindex_byperson",
    )


async def search_byperson(
    client: httpx.AsyncClient,
    query: str,
    *,
    match_type: MatchType,
    max_results: int = 40,
    lookback_hours: int = 26,
) -> list[EpisodeCandidate]:
    """Call PodcastIndex /search/byperson and filter to the last `lookback_hours`.

    NOTE: `byperson` searches person tags, episode title, episode description,
    feed owner, and feed author. It is the correct endpoint for both named-people
    and company-alias searches. `byterm` returns feeds, not episodes — don't use it.
    """
    params = {"q": query, "max": max_results, "fulltext": ""}
    headers = _podcastindex_headers()
    resp = await client.get(
        f"{PODCASTINDEX_BASE}/search/byperson",
        params=params,
        headers=headers,
        timeout=DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items") or []
    cutoff = int(time.time()) - lookback_hours * 3600
    candidates: list[EpisodeCandidate] = []
    for item in items:
        if (item.get("datePublished") or 0) < cutoff:
            continue
        candidates.append(_episode_from_podcastindex_item(item, match_type, query))
    return candidates


# --- iTunes Search ----------------------------------------------------------

def _episode_from_itunes_result(
    item: dict[str, Any],
    match_type: MatchType,
    match_query: str,
) -> EpisodeCandidate | None:
    """iTunes results give us track + collection metadata but no audio enclosure URL.
    Use trackId as GUID and trackViewUrl as episode_url. We can enrich with the
    RSS feed (feedUrl) later if Whisper transcription is needed.
    """
    track_id = item.get("trackId")
    if not track_id:
        return None
    release = item.get("releaseDate")
    if not release:
        return None
    try:
        published_at = datetime.fromisoformat(release.replace("Z", "+00:00"))
    except ValueError:
        return None
    duration_ms = item.get("trackTimeMillis") or 0
    return EpisodeCandidate(
        guid=f"itunes:{track_id}",
        title=item.get("trackName") or "",
        description=item.get("description") or item.get("longDescription") or "",
        podcast=item.get("collectionName") or "",
        podcast_feed_id=None,
        published_at=published_at,
        duration_minutes=duration_ms / 60_000.0,
        episode_url=item.get("trackViewUrl") or "",
        audio_url=item.get("episodeUrl"),  # iTunes sometimes exposes a direct mp3 here
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type=match_type,
        match_query=match_query,
        discovered_via="itunes_search",
    )


async def search_itunes(
    client: httpx.AsyncClient,
    query: str,
    *,
    match_type: MatchType,
    max_results: int = 25,
    lookback_hours: int = 26,
) -> list[EpisodeCandidate]:
    """iTunes Search API — free, no auth, true full-text episode search."""
    params = {
        "term": query,
        "entity": "podcastEpisode",
        "media": "podcast",
        "limit": max_results,
    }
    resp = await client.get(
        ITUNES_SEARCH_BASE,
        params=params,
        timeout=DEFAULT_TIMEOUT,
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    items = resp.json().get("results") or []
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    candidates: list[EpisodeCandidate] = []
    for item in items:
        cand = _episode_from_itunes_result(item, match_type, query)
        if cand is None:
            continue
        if cand.published_at.timestamp() < cutoff:
            continue
        candidates.append(cand)
    return candidates


async def discover_by_people(
    people: list[str],
    *,
    lookback_hours: int = 26,
    concurrency: int = 10,
) -> list[EpisodeCandidate]:
    """Fan out byperson queries across all named people in the watchlist."""
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def _one(name: str) -> list[EpisodeCandidate]:
            async with semaphore:
                try:
                    return await search_byperson(
                        client, name,
                        match_type="named_person",
                        lookback_hours=lookback_hours,
                    )
                except httpx.HTTPError as e:
                    logger.warning("byperson failed for %s: %s", name, e)
                    return []

        results = await asyncio.gather(*(_one(n) for n in people))
    return [c for batch in results for c in batch]


async def discover_by_companies(
    companies: list[str],
    *,
    lookback_hours: int = 26,
    concurrency: int = 10,
) -> list[EpisodeCandidate]:
    """Fan out byperson queries across company names + aliases (it's full-text-ish)."""
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def _one(name: str) -> list[EpisodeCandidate]:
            async with semaphore:
                try:
                    return await search_byperson(
                        client, name,
                        match_type="company",
                        lookback_hours=lookback_hours,
                    )
                except httpx.HTTPError as e:
                    logger.warning("byperson failed for %s: %s", name, e)
                    return []

        results = await asyncio.gather(*(_one(n) for n in companies))
    return [c for batch in results for c in batch]


def _episode_from_rss_entry(entry, podcast_name: str) -> EpisodeCandidate | None:
    guid = entry.get("id") or entry.get("guid")
    if not guid:
        return None
    pub = entry.get("published_parsed")
    if pub is None:
        return None
    published_at = datetime.fromtimestamp(calendar.timegm(pub), tz=timezone.utc)
    duration_str = entry.get("itunes_duration") or ""
    duration_minutes = _parse_itunes_duration(duration_str)
    audio_url = None
    if entry.get("enclosures"):
        audio_url = entry.enclosures[0].get("href")
    return EpisodeCandidate(
        guid=str(guid),
        title=entry.get("title") or "",
        description=entry.get("itunes_summary") or entry.get("summary") or "",
        podcast=podcast_name,
        podcast_feed_id=None,
        published_at=published_at,
        duration_minutes=duration_minutes,
        episode_url=entry.get("link") or audio_url or "",
        audio_url=audio_url,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type="specific_podcast",
        match_query=podcast_name,
        discovered_via="rss",
    )


def _parse_itunes_duration(s: str) -> float:
    if not s:
        return 0.0
    if ":" in s:
        parts = [int(p) for p in s.split(":") if p.isdigit()]
        if len(parts) == 3:
            h, m, sec = parts
            return h * 60 + m + sec / 60
        if len(parts) == 2:
            m, sec = parts
            return m + sec / 60
    try:
        return float(s) / 60
    except ValueError:
        return 0.0


def poll_rss_feed(feed_url: str, podcast_name: str, *, lookback_hours: int = 26) -> list[EpisodeCandidate]:
    """Parse a single RSS feed and return episodes published in the lookback window."""
    parsed = feedparser.parse(feed_url, agent=USER_AGENT)
    if getattr(parsed, "bozo", False) and parsed.bozo_exception:
        logger.warning("feed parse warning for %s: %s", feed_url, parsed.bozo_exception)
    cutoff = datetime.now(timezone.utc).timestamp() - lookback_hours * 3600
    candidates: list[EpisodeCandidate] = []
    for entry in parsed.entries:
        candidate = _episode_from_rss_entry(entry, podcast_name)
        if not candidate:
            continue
        if candidate.published_at.timestamp() < cutoff:
            continue
        candidates.append(candidate)
    return candidates


def dedupe(candidates: list[EpisodeCandidate]) -> list[EpisodeCandidate]:
    """Dedupe by GUID, preferring earlier (more authoritative) discovery surfaces."""
    seen: dict[str, EpisodeCandidate] = {}
    for c in candidates:
        if c.guid not in seen:
            seen[c.guid] = c
    return list(seen.values())


async def discover_all(
    watchlist: Watchlist,
    *,
    lookback_hours: int = 26,
    already_seen: set[str] | None = None,
) -> list[EpisodeCandidate]:
    """Run all three discovery surfaces, dedupe, drop already-seen."""
    company_queries: list[str] = []
    for company in watchlist.companies:
        company_queries.extend(company.all_names)

    people_task = discover_by_people(watchlist.people, lookback_hours=lookback_hours)
    companies_task = discover_by_companies(company_queries, lookback_hours=lookback_hours)
    rss_candidates: list[EpisodeCandidate] = []
    for podcast in watchlist.podcasts:
        try:
            rss_candidates.extend(poll_rss_feed(
                podcast.feed_url, podcast.name, lookback_hours=lookback_hours,
            ))
        except Exception as e:  # noqa: BLE001 — feed parsing can raise many things
            logger.warning("rss poll failed for %s: %s", podcast.name, e)

    people_candidates, company_candidates = await asyncio.gather(
        people_task, companies_task,
    )

    all_candidates = rss_candidates + people_candidates + company_candidates
    deduped = dedupe(all_candidates)
    if already_seen:
        deduped = [c for c in deduped if c.guid not in already_seen]
    logger.info(
        "discovery complete",
        extra={
            "total_candidates": len(all_candidates),
            "deduped": len(deduped),
            "rss_count": len(rss_candidates),
            "people_count": len(people_candidates),
            "company_count": len(company_candidates),
        },
    )
    return deduped
