"""Shared dataclasses used across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal


MatchType = Literal["specific_podcast", "named_person", "company"]
TranscriptSource = Literal[
    "podcast_namespace",
    "official_scrape",
    "youtube_captions",
    "whisper",
    "unavailable",
]
FilterClassification = Literal["guest", "mention", "unrelated"]


@dataclass
class EpisodeCandidate:
    """Raw discovery result before filter pass."""

    guid: str
    title: str
    description: str
    podcast: str
    podcast_feed_id: int | None
    published_at: datetime
    duration_minutes: float
    episode_url: str
    audio_url: str | None
    youtube_url: str | None
    podcast_transcript_url: str | None
    podcast_transcript_type: str | None
    match_type: MatchType
    match_query: str  # the name or company alias that produced this candidate
    discovered_via: str  # "podcastindex_byperson" / "rss" / "exa"
    spotify_url: str | None = None  # enriched later via Spotify Web API


@dataclass
class Episode:
    """A candidate that survived the filter pass."""

    candidate: EpisodeCandidate
    filter_confidence: float  # 0-1
    priority_score: float


@dataclass
class Transcript:
    text: str
    source: TranscriptSource
    language: str = "en"
    confidence: float = 1.0  # Whisper sets this from avg_logprob


@dataclass
class SummaryPoint:
    n: int
    point: str


@dataclass
class Summary:
    bullets: list[SummaryPoint]
    guests: list[str] = field(default_factory=list)
    guest_role_and_company: str = ""
    transcript_completeness: str = "complete"  # complete / partial / low_quality


@dataclass
class DigestEntry:
    """One episode's slot in the digest email."""

    episode: Episode
    transcript: Transcript | None
    summary: Summary | None
    error: str | None = None  # populated on processing failure

    @property
    def is_link_only(self) -> bool:
        return self.summary is None


@dataclass
class RenderedDigest:
    subject: str
    html: str
    text: str
