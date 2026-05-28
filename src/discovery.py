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
    max_retries: int = 3,
) -> list[EpisodeCandidate]:
    """iTunes Search API — free, no auth, true full-text episode search.

    Apple rate-limits aggressively (403/429 when fanning out widely). Retries
    with exponential backoff on 4xx-rate-limit and 5xx.
    """
    params = {
        "term": query,
        "entity": "podcastEpisode",
        "media": "podcast",
        "limit": max_results,
    }
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = await client.get(
                ITUNES_SEARCH_BASE,
                params=params,
                timeout=DEFAULT_TIMEOUT,
                headers={"User-Agent": USER_AGENT},
            )
            if resp.status_code in (403, 429, 500, 502, 503, 504):
                if attempt < max_retries:
                    backoff = 1.0 * (2 ** attempt)
                    await asyncio.sleep(backoff)
                    continue
            resp.raise_for_status()
            break
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_retries:
                await asyncio.sleep(1.0 * (2 ** attempt))
                continue
            raise
    else:  # pragma: no cover — for/else not entered when break fires
        if last_exc:
            raise last_exc
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


ITUNES_BASE_DELAY_SEC = 0.2  # spread requests to ~5 RPS effective rate


async def _discover_via_itunes(
    queries: list[str],
    *,
    match_type: MatchType,
    lookback_hours: int,
    concurrency: int = 2,
) -> list[EpisodeCandidate]:
    """Fan out iTunes Search queries with a semaphore. Handles errors per-query.

    Apple's iTunes Search rate-limits aggressively — production runs hit 429s
    at concurrency=3 within the first second. concurrency=2 with a 200ms base
    delay per request gives ~5 effective RPS, well under Apple's threshold.
    Per-query retry-with-backoff still handles transient 429s.
    """
    semaphore = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient() as client:
        async def _one(name: str) -> list[EpisodeCandidate]:
            async with semaphore:
                await asyncio.sleep(ITUNES_BASE_DELAY_SEC)
                try:
                    return await search_itunes(
                        client, name,
                        match_type=match_type,
                        lookback_hours=lookback_hours,
                    )
                except httpx.HTTPError as e:
                    logger.warning("itunes search failed for %r: %s", name, e)
                    return []

        results = await asyncio.gather(*(_one(n) for n in queries))
    return [c for batch in results for c in batch]


async def discover_by_people(
    people: list[str],
    *,
    lookback_hours: int = 26,
    concurrency: int = 10,
) -> list[EpisodeCandidate]:
    """Fan out iTunes Search across all named people in the watchlist."""
    return await _discover_via_itunes(
        people,
        match_type="named_person",
        lookback_hours=lookback_hours,
        concurrency=concurrency,
    )


async def discover_by_companies(
    companies: list[str],
    *,
    lookback_hours: int = 26,
    concurrency: int = 10,
) -> list[EpisodeCandidate]:
    """Fan out iTunes Search across company names + aliases."""
    return await _discover_via_itunes(
        companies,
        match_type="company",
        lookback_hours=lookback_hours,
        concurrency=concurrency,
    )


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
    """Parse a single RSS feed and return episodes published in the lookback window.

    Fetches with httpx (which uses certifi for TLS) then hands the raw bytes to
    feedparser. This avoids the macOS stdlib-urllib cert verification failure
    that feedparser.parse(url) hits in some dev environments.
    """
    try:
        with httpx.Client(timeout=DEFAULT_TIMEOUT, follow_redirects=True) as client:
            resp = client.get(feed_url, headers={"User-Agent": USER_AGENT})
            resp.raise_for_status()
            feed_bytes = resp.content
    except httpx.HTTPError as e:
        logger.warning("rss fetch failed for %s: %s", feed_url, e)
        return []

    parsed = feedparser.parse(feed_bytes)
    if getattr(parsed, "bozo", False) and parsed.bozo_exception:
        # feedparser flags many minor issues as bozo; only warn at debug-level
        logger.debug("feed parse warning for %s: %s", feed_url, parsed.bozo_exception)
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


def _normalize_for_dedup(s: str) -> str:
    """Lowercase + strip punctuation/whitespace for fuzzy episode matching."""
    import re
    s = (s or "").lower()
    # Collapse runs of non-alphanumeric to a single space, then strip
    s = re.sub(r"[^\w]+", " ", s).strip()
    return s


def _dedup_key(c: EpisodeCandidate) -> str:
    """Cross-surface dedup key: normalized (podcast, title prefix).

    Same episode appearing via RSS + iTunes person search has different GUIDs
    (RSS guid vs itunes:trackId) but the (podcast, title) pair is stable.
    Title prefix capped at 80 chars to absorb minor formatting differences
    like trailing "[EP.473]" suffixes.
    """
    podcast = _normalize_for_dedup(c.podcast)
    title = _normalize_for_dedup(c.title)[:80]
    return f"{podcast}|{title}"


def _is_english_ish(s: str) -> bool:
    """Filter out predominantly non-Latin-script titles (Arabic, CJK, Cyrillic, etc.).

    iTunes US store returns podcasts in many languages. This is a coarse
    heuristic: count Latin letters vs other letters in the string. We want
    >= 70% of letter chars to be Latin script.
    """
    if not s:
        return True  # don't filter empty; let downstream decide
    latin = 0
    other_letter = 0
    for ch in s:
        if ch.isalpha():
            # Latin if it's in the basic Latin or Latin-1 supplement range
            if ord(ch) < 0x180 or 0x1E00 <= ord(ch) <= 0x1EFF:
                latin += 1
            else:
                other_letter += 1
    total_letters = latin + other_letter
    if total_letters == 0:
        return True  # e.g. "Ep. 47 - The Show" — accept
    return (latin / total_letters) >= 0.7


def dedupe(candidates: list[EpisodeCandidate]) -> list[EpisodeCandidate]:
    """Dedupe by GUID and by (podcast, title-prefix).

    Within a dedup-key collision, prefer:
      1. specific_podcast > named_person > company (more authoritative match)
      2. RSS-discovered > iTunes-discovered (RSS GUID is canonical)
    """
    by_guid: dict[str, EpisodeCandidate] = {}
    for c in candidates:
        if c.guid not in by_guid:
            by_guid[c.guid] = c

    # Now collapse cross-surface duplicates by (podcast, title)
    tier_rank = {"specific_podcast": 0, "named_person": 1, "company": 2}
    source_rank = {"rss": 0, "podcastindex_byperson": 1, "itunes_search": 2}

    def _rank(c: EpisodeCandidate) -> tuple[int, int]:
        return (
            tier_rank.get(c.match_type, 3),
            source_rank.get(c.discovered_via, 3),
        )

    by_key: dict[str, EpisodeCandidate] = {}
    for c in by_guid.values():
        key = _dedup_key(c)
        existing = by_key.get(key)
        if existing is None or _rank(c) < _rank(existing):
            by_key[key] = c
    return list(by_key.values())


def filter_english(candidates: list[EpisodeCandidate]) -> list[EpisodeCandidate]:
    """Drop candidates whose title or podcast name is predominantly non-Latin."""
    out: list[EpisodeCandidate] = []
    dropped = 0
    for c in candidates:
        # Title is the primary signal; podcast name is secondary
        if _is_english_ish(c.title) and _is_english_ish(c.podcast):
            out.append(c)
        else:
            dropped += 1
    if dropped:
        logger.info("filtered %d non-English candidate(s)", dropped)
    return out


class DiscoveryTotalFailure(RuntimeError):
    """Raised when ALL discovery surfaces failed. Triggers the failure-digest path."""


async def discover_all(
    watchlist: Watchlist,
    *,
    lookback_hours: int = 26,
    already_seen: set[str] | None = None,
) -> list[EpisodeCandidate]:
    """Run all three discovery surfaces, dedupe, drop already-seen.

    Raises DiscoveryTotalFailure if every surface failed (so the caller can send
    a "discovery failed today" digest instead of an empty one).
    """
    company_queries: list[str] = []
    for company in watchlist.companies:
        company_queries.extend(company.all_names)

    rss_candidates: list[EpisodeCandidate] = []
    rss_failures = 0
    for podcast in watchlist.podcasts:
        try:
            rss_candidates.extend(poll_rss_feed(
                podcast.feed_url, podcast.name, lookback_hours=lookback_hours,
            ))
        except Exception as e:  # noqa: BLE001 — feed parsing can raise many things
            logger.warning("rss poll failed for %s: %s", podcast.name, e)
            rss_failures += 1
    rss_total_fail = (
        len(watchlist.podcasts) > 0 and rss_failures == len(watchlist.podcasts)
    )

    try:
        people_candidates = await discover_by_people(
            watchlist.people, lookback_hours=lookback_hours,
        )
        people_failed = False
    except Exception as e:  # noqa: BLE001
        logger.warning("people discovery surface failed: %s", e)
        people_candidates = []
        people_failed = True

    try:
        company_candidates = await discover_by_companies(
            company_queries, lookback_hours=lookback_hours,
        )
        company_failed = False
    except Exception as e:  # noqa: BLE001
        logger.warning("company discovery surface failed: %s", e)
        company_candidates = []
        company_failed = True

    if rss_total_fail and people_failed and company_failed:
        raise DiscoveryTotalFailure("all discovery surfaces failed")

    all_candidates = rss_candidates + people_candidates + company_candidates
    deduped = dedupe(all_candidates)
    pre_seen_count = len(deduped)
    deduped = filter_english(deduped)
    post_english_count = len(deduped)
    if already_seen:
        deduped = [c for c in deduped if c.guid not in already_seen]
    logger.info(
        "discovery complete: rss=%d people=%d company=%d -> deduped=%d "
        "-> english=%d -> after-seen=%d",
        len(rss_candidates),
        len(people_candidates),
        len(company_candidates),
        pre_seen_count,
        post_english_count,
        len(deduped),
    )
    return deduped
