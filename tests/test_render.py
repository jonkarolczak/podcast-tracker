"""Email rendering: subject, HTML escape, structure."""
from datetime import date, datetime, timezone

import pytest

from src.models import (
    DigestEntry,
    Episode,
    EpisodeCandidate,
    Summary,
    SummaryPoint,
    Transcript,
)
from src.render_email import render


def _candidate(title: str = "Sam Altman on OpenAI", podcast: str = "Dwarkesh Podcast") -> EpisodeCandidate:
    return EpisodeCandidate(
        guid="g1",
        title=title,
        description="x",
        podcast=podcast,
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        duration_minutes=120.0,
        episode_url="https://example.com/ep1",
        audio_url=None,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type="named_person",
        match_query="Sam Altman",
        discovered_via="podcastindex_byperson",
    )


def _entry_with_summary() -> DigestEntry:
    candidate = _candidate()
    episode = Episode(candidate=candidate, filter_confidence=0.95, priority_score=110.0)
    summary = Summary(
        headline="GPT-6 ships in Q3 with native agentic capabilities.",
        bullets=[
            SummaryPoint(n=1, category="STRATEGY", point="Altman commits to a Q3 GPT-6 launch.", segment="beginning"),
            SummaryPoint(n=2, category="HIRING", point="OpenAI added 200 researchers in 2026.", segment="middle"),
        ],
        open_questions=["When does the agentic API GA?"],
    )
    return DigestEntry(
        episode=episode,
        transcript=Transcript(text="x", source="youtube_captions"),
        summary=summary,
    )


def test_subject_with_entries():
    entries = [_entry_with_summary()]
    rendered = render(entries, date(2026, 5, 23))
    assert "1 new" in rendered.subject
    assert "2026-05-23" in rendered.subject


def test_subject_when_empty_uses_no_new_matches_wording():
    rendered = render([], date(2026, 5, 23))
    # Gmail mildly penalizes literal "0" in subject — confirm we avoid it
    assert "no new matches" in rendered.subject
    assert " 0 " not in rendered.subject
    assert " 0 new" not in rendered.subject


def test_html_escapes_titles_with_html_chars():
    candidate = _candidate(title="Sam Altman <script>alert('xss')</script>")
    episode = Episode(candidate=candidate, filter_confidence=1.0, priority_score=50.0)
    entry = DigestEntry(episode=episode, transcript=None, summary=None)
    rendered = render([entry], date(2026, 5, 23))
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


def test_link_only_when_no_summary():
    candidate = _candidate()
    episode = Episode(candidate=candidate, filter_confidence=1.0, priority_score=50.0)
    entry = DigestEntry(episode=episode, transcript=None, summary=None)
    rendered = render([entry], date(2026, 5, 23))
    assert "Transcript unavailable" in rendered.html
    assert candidate.episode_url in rendered.html


def test_html_includes_dark_mode_css():
    entries = [_entry_with_summary()]
    rendered = render(entries, date(2026, 5, 23))
    # css-inline with keep_style_tags=True preserves the @media block
    assert "prefers-color-scheme" in rendered.html
    assert "color-scheme" in rendered.html


def test_plain_text_includes_all_bullets():
    entries = [_entry_with_summary()]
    rendered = render(entries, date(2026, 5, 23))
    assert "Altman commits to a Q3 GPT-6 launch." in rendered.text
    assert "OpenAI added 200 researchers in 2026." in rendered.text
    assert "STRATEGY" in rendered.text
    assert "HIRING" in rendered.text
