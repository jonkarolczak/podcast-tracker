"""Discovery surface unit tests (data shape + dedup + failure handling).

Network calls are mocked. The real PodcastIndex/iTunes/RSS flows are exercised
by the smoke-test CLI.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.config import Company, MatchPriority, Podcast, TierWeights, Watchlist
from src.discovery import (
    DiscoveryTotalFailure,
    _episode_from_itunes_result,
    _is_english_ish,
    _parse_itunes_duration,
    dedupe,
    discover_all,
    filter_english,
)
from src.models import EpisodeCandidate


def test_parse_itunes_duration_hms():
    assert _parse_itunes_duration("01:30:45") == pytest.approx(90.75, rel=1e-3)


def test_parse_itunes_duration_mmss():
    assert _parse_itunes_duration("39:42") == pytest.approx(39.7, rel=1e-3)


def test_parse_itunes_duration_seconds_string():
    assert _parse_itunes_duration("2400") == 40.0


def test_parse_itunes_duration_empty():
    assert _parse_itunes_duration("") == 0.0


def test_parse_itunes_duration_garbage():
    assert _parse_itunes_duration("not a duration") == 0.0


def _itunes_payload(**overrides):
    base = {
        "trackId": 12345,
        "trackName": "Sam Altman on AI",
        "collectionName": "Some Podcast",
        "releaseDate": "2026-05-22T10:00:00Z",
        "trackTimeMillis": 3600_000,
        "trackViewUrl": "https://podcasts.apple.com/us/podcast/x",
        "description": "Description here",
    }
    base.update(overrides)
    return base


def test_episode_from_itunes_normal_path():
    candidate = _episode_from_itunes_result(
        _itunes_payload(),
        match_type="named_person",
        match_query="Sam Altman",
    )
    assert candidate is not None
    assert candidate.guid == "itunes:12345"
    assert candidate.title == "Sam Altman on AI"
    assert candidate.podcast == "Some Podcast"
    assert candidate.match_type == "named_person"
    assert candidate.duration_minutes == 60.0


def test_episode_from_itunes_missing_track_id_returns_none():
    payload = _itunes_payload()
    del payload["trackId"]
    assert _episode_from_itunes_result(payload, "named_person", "x") is None


def test_episode_from_itunes_unparseable_date_returns_none():
    payload = _itunes_payload(releaseDate="not-a-date")
    assert _episode_from_itunes_result(payload, "named_person", "x") is None


def _make_cand(
    *, guid: str, podcast: str, title: str = "t",
    match_type: str = "named_person",
    discovered_via: str = "itunes_search",
) -> EpisodeCandidate:
    return EpisodeCandidate(
        guid=guid, title=title, description="", podcast=podcast,
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        duration_minutes=10.0, episode_url="https://x", audio_url=None,
        youtube_url=None, podcast_transcript_url=None,
        podcast_transcript_type=None, match_type=match_type,
        match_query="x", discovered_via=discovered_via,
    )


def test_dedupe_by_guid_keeps_first():
    out = dedupe([
        _make_cand(guid="g1", podcast="first"),
        _make_cand(guid="g1", podcast="second"),
        _make_cand(guid="g2", podcast="third"),
    ])
    assert len(out) == 2


def test_dedupe_cross_surface_same_episode():
    """RSS guid + itunes:trackId for the same Gavin Baker episode should collapse to one."""
    rss = _make_cand(
        guid="megaphone-abc123",
        podcast="Invest Like the Best with Patrick O'Shaughnessy",
        title="Gavin Baker - Watts and Wafers - [Invest Like the Best, EP.473]",
        match_type="specific_podcast",
        discovered_via="rss",
    )
    itunes = _make_cand(
        guid="itunes:1000769000001",
        podcast="Invest Like the Best with Patrick O'Shaughnessy",
        title="Gavin Baker - Watts and Wafers - [Invest Like the Best, EP.473]",
        match_type="named_person",
        discovered_via="itunes_search",
    )
    out = dedupe([rss, itunes])
    assert len(out) == 1
    # RSS source preferred (rank 0) over iTunes (rank 2)
    assert out[0].discovered_via == "rss"
    assert out[0].match_type == "specific_podcast"


def test_dedupe_cross_surface_prefers_specific_podcast():
    """A named-person hit and a company hit on the same episode should keep the higher-tier."""
    person = _make_cand(
        guid="p1", podcast="X", title="same title",
        match_type="named_person", discovered_via="itunes_search",
    )
    company = _make_cand(
        guid="c1", podcast="X", title="same title",
        match_type="company", discovered_via="itunes_search",
    )
    out = dedupe([person, company])
    assert len(out) == 1
    assert out[0].match_type == "named_person"


def test_dedupe_different_episodes_kept():
    a = _make_cand(guid="g1", podcast="Show", title="Episode One")
    b = _make_cand(guid="g2", podcast="Show", title="Episode Two")
    out = dedupe([a, b])
    assert len(out) == 2


def test_is_english_ish_accepts_latin():
    assert _is_english_ish("Sam Altman on the future of AI")
    assert _is_english_ish("Café conversation about startups")  # Latin-1 accent OK


def test_is_english_ish_rejects_cjk():
    assert not _is_english_ish("人工智能的未来")  # Chinese
    assert not _is_english_ish("AIの未来について話す")  # Japanese-heavy


def test_is_english_ish_rejects_arabic():
    assert not _is_english_ish("الذكاء الاصطناعي والمستقبل")


def test_is_english_ish_rejects_cyrillic():
    assert not _is_english_ish("Беседа об искусственном интеллекте")


def test_is_english_ish_handles_no_letters():
    """'Ep. 47' style titles with only digits and punctuation pass through."""
    assert _is_english_ish("47")
    assert _is_english_ish("")


def test_filter_english_drops_non_english():
    eng = _make_cand(guid="g1", podcast="The Show", title="Sam Altman on AI")
    non_eng = _make_cand(guid="g2", podcast="播客", title="人工智能讨论")
    out = filter_english([eng, non_eng])
    assert len(out) == 1
    assert out[0].guid == "g1"


@pytest.mark.asyncio
async def test_discover_all_raises_when_all_surfaces_fail():
    watchlist = Watchlist(
        companies=[Company(name="OpenAI")],
        people=["Sam Altman"],
        podcasts=[Podcast(name="Test", feed_url="https://not-a-real-feed.invalid/feed.rss")],
        match_priority=MatchPriority(tier_weights=TierWeights()),
    )
    with patch("src.discovery.discover_by_people", new=AsyncMock(side_effect=RuntimeError("itunes down"))), \
         patch("src.discovery.discover_by_companies", new=AsyncMock(side_effect=RuntimeError("itunes down"))), \
         patch("src.discovery.poll_rss_feed", side_effect=RuntimeError("rss down")):
        with pytest.raises(DiscoveryTotalFailure):
            await discover_all(watchlist, lookback_hours=24)


@pytest.mark.asyncio
async def test_discover_all_succeeds_when_only_one_surface_fails():
    watchlist = Watchlist(
        companies=[Company(name="OpenAI")],
        people=["Sam Altman"],
        podcasts=[Podcast(name="Test", feed_url="https://not-a-real-feed.invalid/feed.rss")],
        match_priority=MatchPriority(tier_weights=TierWeights()),
    )
    fake_candidate = EpisodeCandidate(
        guid="itunes:1", title="t", description="", podcast="p",
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        duration_minutes=10.0, episode_url="https://x", audio_url=None,
        youtube_url=None, podcast_transcript_url=None,
        podcast_transcript_type=None, match_type="named_person",
        match_query="Sam Altman", discovered_via="itunes_search",
    )
    with patch("src.discovery.discover_by_people", new=AsyncMock(return_value=[fake_candidate])), \
         patch("src.discovery.discover_by_companies", new=AsyncMock(side_effect=RuntimeError("itunes down"))), \
         patch("src.discovery.poll_rss_feed", side_effect=RuntimeError("rss down")):
        result = await discover_all(watchlist, lookback_hours=24)
        assert len(result) == 1
        assert result[0].guid == "itunes:1"
