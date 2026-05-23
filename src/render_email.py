"""Render the daily digest into HTML + plain-text + a subject line."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import css_inline
from jinja2 import Environment, FileSystemLoader

from .models import DigestEntry, RenderedDigest

TEMPLATE_DIR = Path("templates")


def _autoescape_for(template_name: str | None) -> bool:
    """Autoescape only HTML templates. Our .html.j2 naming convention isn't caught
    by select_autoescape() which checks just the final extension."""
    if not template_name:
        return False
    return ".html" in template_name


_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=_autoescape_for,
    trim_blocks=True,
    lstrip_blocks=True,
)
_inliner = css_inline.CSSInliner(keep_style_tags=True, keep_link_tags=False)


def _subject(run_date: date, entries: list[DigestEntry]) -> str:
    if not entries:
        return f"Podcast Tracker — {run_date} — no new matches"
    return f"Podcast Tracker — {run_date} — {len(entries)} new"


def render(entries: list[DigestEntry], run_date: date) -> RenderedDigest:
    """Render the digest. Sorts entries by priority_score descending."""
    sorted_entries = sorted(
        entries,
        key=lambda e: e.episode.priority_score,
        reverse=True,
    )
    html_raw = _env.get_template("digest.html.j2").render(
        entries=sorted_entries, run_date=run_date,
    )
    html = _inliner.inline(html_raw)
    text = _env.get_template("digest.txt.j2").render(
        entries=sorted_entries, run_date=run_date,
    )
    return RenderedDigest(
        subject=_subject(run_date, sorted_entries),
        html=html,
        text=text,
    )
