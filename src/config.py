"""Config loaders for watchlist.yaml and settings.yaml. Pydantic-validated at load time."""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator


class Company(BaseModel):
    name: str
    aliases: list[str] = Field(default_factory=list)

    @property
    def all_names(self) -> list[str]:
        return [self.name, *self.aliases]


class Podcast(BaseModel):
    name: str
    feed_url: str


class TierWeights(BaseModel):
    specific_podcast: int = 100
    named_person: int = 50
    company: int = 10


class MatchPriority(BaseModel):
    tier_weights: TierWeights = Field(default_factory=TierWeights)


class Watchlist(BaseModel):
    companies: list[Company]
    people: list[str]
    podcasts: list[Podcast]
    match_priority: MatchPriority = Field(default_factory=MatchPriority)

    @field_validator("people")
    @classmethod
    def _no_empty_people(cls, v: list[str]) -> list[str]:
        cleaned = [p.strip() for p in v if p.strip()]
        if not cleaned:
            raise ValueError("watchlist.people must contain at least one name")
        return cleaned


class Schedule(BaseModel):
    target_local_time: str = "06:00"
    timezone: str = "America/Chicago"
    lookback_hours: int = 26


class Budgets(BaseModel):
    whisper_wallclock_minutes: float = 60.0
    total_wallclock_minutes: float = 75.0
    anthropic_warn_usd: float = 5.00
    anthropic_hard_stop_usd: float = 10.00
    assumed_whisper_rtf: float = 8.0


class TranscriptSettings(BaseModel):
    whisper_model: str = "base.en"
    whisper_compute_type: str = "int8"
    whisper_threads: int = 4


class EmailSettings(BaseModel):
    send_when_empty: bool = True


class Settings(BaseModel):
    schedule: Schedule = Field(default_factory=Schedule)
    budgets: Budgets = Field(default_factory=Budgets)
    transcript: TranscriptSettings = Field(default_factory=TranscriptSettings)
    email: EmailSettings = Field(default_factory=EmailSettings)


def load_watchlist(path: Path | str = "config/watchlist.yaml") -> Watchlist:
    with open(path) as f:
        return Watchlist.model_validate(yaml.safe_load(f))


def load_settings(path: Path | str = "config/settings.yaml") -> Settings:
    with open(path) as f:
        return Settings.model_validate(yaml.safe_load(f))
