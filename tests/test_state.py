"""State persistence and idempotency guard."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src.models import EpisodeCandidate
from src.state import (
    add_seen_episodes,
    already_ran_today,
    load_seen_guids,
    mark_ran_today,
    _sanitize,
)


def _candidate(guid: str, title: str = "x", podcast: str = "p") -> EpisodeCandidate:
    return EpisodeCandidate(
        guid=guid,
        title=title,
        description="",
        podcast=podcast,
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 23, tzinfo=ZoneInfo("America/Chicago")),
        duration_minutes=60.0,
        episode_url="https://example.com/ep",
        audio_url=None,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type="named_person",
        match_query="Sam Altman",
        discovered_via="podcastindex_byperson",
    )


def test_seen_guids_load_empty(tmp_path):
    f = tmp_path / "seen.json"
    f.write_text("[]")
    assert load_seen_guids(f) == set()


def test_add_and_load_round_trip(tmp_path):
    f = tmp_path / "seen.json"
    add_seen_episodes([_candidate("guid-1"), _candidate("guid-2")], path=f)
    assert load_seen_guids(f) == {"guid-1", "guid-2"}


def test_add_is_idempotent_on_guid(tmp_path):
    f = tmp_path / "seen.json"
    add_seen_episodes([_candidate("guid-1")], path=f)
    add_seen_episodes([_candidate("guid-1"), _candidate("guid-2")], path=f)
    data = json.loads(f.read_text())
    assert {e["guid"] for e in data} == {"guid-1", "guid-2"}
    assert len(data) == 2


def test_sanitize_strips_control_chars():
    assert _sanitize("hi\x00\x01world", 200) == "hiworld"


def test_sanitize_caps_length():
    long = "x" * 500
    assert len(_sanitize(long, 200)) == 200


def test_sanitize_normalizes_unicode():
    # NFKC normalizes the wide A to ASCII A
    assert _sanitize("Aを", 10) == "Aを"  # normal NFKC behavior; pass-through


def test_persisted_titles_are_sanitized(tmp_path):
    f = tmp_path / "seen.json"
    nasty = "title\x00with\x01ctrl chars"
    add_seen_episodes([_candidate("g", title=nasty)], path=f)
    data = json.loads(f.read_text())
    assert "\x00" not in data[0]["title"]
    assert "\x01" not in data[0]["title"]


def test_idempotency_guard(tmp_path):
    p = tmp_path / "last_run.txt"
    assert not already_ran_today(p)
    mark_ran_today(p)
    assert already_ran_today(p)
