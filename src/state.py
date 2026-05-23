"""State management: seen-episodes JSON + last-run idempotency guard.

State is committed back to the repo by the GHA workflow on successful run.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .models import EpisodeCandidate

CHICAGO = ZoneInfo("America/Chicago")
SEEN_PATH = Path("state/seen_episodes.json")
LAST_RUN_PATH = Path("state/last_run.txt")

_TITLE_CAP = 200
_PODCAST_CAP = 100


def _sanitize(s: str, cap: int) -> str:
    """NFKC normalize, strip control chars, length-cap. Prevents malicious feed
    strings from polluting git history.
    """
    s = unicodedata.normalize("NFKC", s or "")
    s = "".join(c for c in s if c.isprintable() or c == " ")
    return s[:cap]


def load_seen_guids(path: Path = SEEN_PATH) -> set[str]:
    if not path.exists():
        return set()
    with open(path) as f:
        data = json.load(f)
    return {entry["guid"] for entry in data}


def add_seen_episodes(
    candidates: list[EpisodeCandidate],
    *,
    path: Path = SEEN_PATH,
    now: datetime | None = None,
) -> None:
    """Append candidates to the seen set. Idempotent on GUID."""
    now = now or datetime.now(CHICAGO)
    existing: list[dict] = []
    if path.exists():
        with open(path) as f:
            existing = json.load(f)
    seen = {entry["guid"] for entry in existing}
    for c in candidates:
        if c.guid in seen:
            continue
        existing.append({
            "guid": c.guid,
            "first_seen": now.isoformat(),
            "title": _sanitize(c.title, _TITLE_CAP),
            "podcast": _sanitize(c.podcast, _PODCAST_CAP),
        })
        seen.add(c.guid)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(existing, f, indent=2, ensure_ascii=False)
        f.write("\n")


def already_ran_today(path: Path = LAST_RUN_PATH, *, now: datetime | None = None) -> bool:
    if not path.exists():
        return False
    last = path.read_text().strip()
    if not last:
        return False
    today = (now or datetime.now(CHICAGO)).date().isoformat()
    return last == today


def mark_ran_today(path: Path = LAST_RUN_PATH, *, now: datetime | None = None) -> None:
    today = (now or datetime.now(CHICAGO)).date().isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(today + "\n")
