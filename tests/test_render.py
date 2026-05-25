"""Email rendering: subject, HTML escape, format compliance."""
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


def _candidate(
    title: str = "Sam Altman on OpenAI",
    podcast: str = "The Tucker Carlson Show",
    match_query: str = "Sam Altman",
    match_type: str = "named_person",
) -> EpisodeCandidate:
    return EpisodeCandidate(
        guid="g1",
        title=title,
        description="x",
        podcast=podcast,
        podcast_feed_id=None,
        published_at=datetime(2026, 5, 22, tzinfo=timezone.utc),
        duration_minutes=40.0,
        episode_url="https://example.com/ep1",
        audio_url=None,
        youtube_url=None,
        podcast_transcript_url=None,
        podcast_transcript_type=None,
        match_type=match_type,
        match_query=match_query,
        discovered_via="itunes_search",
    )


def _entry_with_summary(
    candidate: EpisodeCandidate | None = None,
    guests: list[str] | None = None,
    role: str = "CEO of OpenAI",
) -> DigestEntry:
    candidate = candidate or _candidate()
    episode = Episode(candidate=candidate, filter_confidence=0.95, priority_score=110.0)
    summary = Summary(
        bullets=[
            SummaryPoint(n=1, point="Altman commits to a Q3 GPT-6 launch."),
            SummaryPoint(n=2, point="OpenAI added 200 researchers in 2026."),
        ],
        guests=guests if guests is not None else ["Sam Altman"],
        guest_role_and_company=role,
    )
    return DigestEntry(
        episode=episode,
        transcript=Transcript(text="x", source="whisper"),
        summary=summary,
    )


def test_subject_format():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    assert rendered.subject == "Podcast Tracker (May 24, 2026)"


def test_subject_format_constant_regardless_of_count():
    """Subject is the same whether 0, 1, or N episodes — count moves to subheading."""
    one = render([_entry_with_summary()], date(2026, 5, 24)).subject
    two = render([_entry_with_summary(), _entry_with_summary()], date(2026, 5, 24)).subject
    zero = render([], date(2026, 5, 24)).subject
    assert one == two == zero == "Podcast Tracker (May 24, 2026)"


def test_heading_in_body_matches_subject():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    assert "Podcast Tracker (May 24, 2026)" in rendered.html
    assert "Podcast Tracker (May 24, 2026)" in rendered.text
    # No "(N Episodes)" appended to heading
    assert "Podcast Tracker (May 24, 2026) (" not in rendered.html
    assert "Podcast Tracker (May 24, 2026) (" not in rendered.text


def test_subheading_shows_count():
    one = render([_entry_with_summary()], date(2026, 5, 24))
    assert "1 Episode" in one.text and "1 Episode" in one.html

    two = render([_entry_with_summary(), _entry_with_summary()], date(2026, 5, 24))
    assert "2 Episodes" in two.text and "2 Episodes" in two.html

    zero = render([], date(2026, 5, 24))
    assert "0 Episodes" in zero.text and "0 Episodes" in zero.html


def test_per_episode_header_with_guest_uses_colon():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    # "## 1. <Podcast>: <Guest>"
    assert "1. The Tucker Carlson Show: Sam Altman" in rendered.text


def test_per_episode_header_company_match_no_colon():
    candidate = _candidate(
        title="A discussion about OpenAI",
        match_query="OpenAI",
        match_type="company",
    )
    entry = _entry_with_summary(candidate=candidate, guests=[], role="")
    rendered = render([entry], date(2026, 5, 24))
    # No colon when there's no guest — "## 1. <Podcast>" alone
    assert "1. The Tucker Carlson Show\n" in rendered.text
    assert "No guest / hosts discussing OpenAI" in rendered.text


def test_multiple_guests_joined_with_commas():
    entry = _entry_with_summary(guests=["Sam Altman", "Greg Brockman", "Mira Murati"])
    rendered = render([entry], date(2026, 5, 24))
    assert "Sam Altman, Greg Brockman, Mira Murati" in rendered.text
    assert "Sam Altman, Greg Brockman, Mira Murati" in rendered.html


def test_summary_heading_is_just_summary():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    # Per Jon's spec — heading is just "Summary", not "N-point summary"
    assert "### Summary" in rendered.text
    assert "Summary" in rendered.html
    assert "-point summary" not in rendered.text
    assert "-point summary" not in rendered.html


def test_guests_label_not_person_interviewed():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    assert "Guests:" in rendered.text
    assert "Person Interviewed" not in rendered.text


def test_spotify_url_used_when_present():
    candidate = _candidate()
    candidate.spotify_url = "https://open.spotify.com/episode/abc123"
    entry = _entry_with_summary(candidate=candidate)
    rendered = render([entry], date(2026, 5, 24))
    assert "https://open.spotify.com/episode/abc123" in rendered.text
    # Apple URL should NOT appear when Spotify is available
    assert "https://example.com/ep1" not in rendered.text


def test_falls_back_to_episode_url_when_spotify_missing():
    entry = _entry_with_summary()
    rendered = render([entry], date(2026, 5, 24))
    # No spotify_url set → use the iTunes/Apple episode_url
    assert "https://example.com/ep1" in rendered.text


def test_no_category_labels_in_summary():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    # Per Jon's spec — no FINANCIAL/STRATEGY/MARKET/QUOTE labels
    for label in ("FINANCIAL", "STRATEGY", "MARKET", "TECHNICAL", "QUOTE", "HIRING"):
        assert label not in rendered.text, f"{label} appeared in plain-text body"


def test_company_role_line_appears_when_present():
    rendered = render([_entry_with_summary(role="CEO of OpenAI")], date(2026, 5, 24))
    assert "Company / role: CEO of OpenAI" in rendered.text
    assert "CEO of OpenAI" in rendered.html


def test_company_role_line_omitted_when_empty():
    rendered = render([_entry_with_summary(role="")], date(2026, 5, 24))
    assert "Company / role:" not in rendered.text


def test_html_escapes_titles_with_html_chars():
    candidate = _candidate(title="Sam Altman <script>alert('xss')</script>")
    entry = _entry_with_summary(candidate=candidate)
    rendered = render([entry], date(2026, 5, 24))
    assert "<script>" not in rendered.html
    assert "&lt;script&gt;" in rendered.html


def test_link_only_when_no_summary():
    candidate = _candidate()
    episode = Episode(candidate=candidate, filter_confidence=1.0, priority_score=50.0)
    entry = DigestEntry(episode=episode, transcript=None, summary=None)
    rendered = render([entry], date(2026, 5, 24))
    assert "Transcript unavailable" in rendered.html
    assert candidate.episode_url in rendered.html


def test_html_includes_dark_mode_css():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    assert "prefers-color-scheme" in rendered.html
    assert "color-scheme" in rendered.html


def test_plain_text_includes_all_bullets():
    rendered = render([_entry_with_summary()], date(2026, 5, 24))
    assert "Altman commits to a Q3 GPT-6 launch." in rendered.text
    assert "OpenAI added 200 researchers in 2026." in rendered.text


