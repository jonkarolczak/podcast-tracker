"""Render the daily digest into HTML + plain-text + a subject line.

Display format (per Jon):
  Subject + heading: "Podcast Tracker (May 24, 2026) (N Episode[s])"
  Per-entry header: "## N. <Podcast>: <Guest>"  (omit ":" and guest when no guest)
  Per-entry metadata block: Podcast / Episode / Person Interviewed / Company / Role / Date / Link
  Then "### 15-point summary" with concise standalone bullets (no category labels).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import css_inline
from jinja2 import Environment, FileSystemLoader

from .models import DigestEntry, RenderedDigest

TEMPLATE_DIR = Path("templates")


def _autoescape_for(template_name: str | None) -> bool:
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


@dataclass
class EntryContext:
    """Pre-computed display fields for one digest entry."""

    entry: DigestEntry
    n: int
    podcast: str
    episode_title: str
    guests_display: str   # "Sam Altman, Dwarkesh Patel" or "No guest / hosts discussing OpenAI" or "—"
    role_and_company: str
    has_guest: bool       # whether to render "Podcast: Guest(s)" header vs just "Podcast"
    header_guest: str     # comma-joined guests for the header line
    date_display: str     # "May 24, 2026"
    primary_url: str      # Spotify URL when available, falls back to Apple/episode URL


def _format_date(d: date) -> str:
    # %-d (no leading zero on day) is BSD/Linux-specific; macOS supports it.
    try:
        return d.strftime("%B %-d, %Y")
    except ValueError:
        # Windows fallback
        return d.strftime("%B %d, %Y").replace(" 0", " ")


def _build_entry_context(entry: DigestEntry, n: int) -> EntryContext:
    c = entry.episode.candidate
    summary = entry.summary

    role_and_company = (summary.guest_role_and_company if summary else "") or ""

    # Determine guests
    llm_guests = list(summary.guests) if summary else []
    if llm_guests:
        guests_display = ", ".join(llm_guests)
        header_guest = guests_display
        has_guest = True
    elif c.match_type == "named_person" and c.match_query:
        guests_display = c.match_query
        header_guest = c.match_query
        has_guest = True
    elif c.match_type == "company" and c.match_query:
        guests_display = f"No guest / hosts discussing {c.match_query}"
        header_guest = ""
        has_guest = False
    else:
        guests_display = "—"
        header_guest = ""
        has_guest = False

    primary_url = c.spotify_url or c.episode_url

    return EntryContext(
        entry=entry,
        n=n,
        podcast=c.podcast,
        episode_title=c.title,
        guests_display=guests_display,
        role_and_company=role_and_company,
        has_guest=has_guest,
        header_guest=header_guest,
        date_display=_format_date(c.published_at.date()),
        primary_url=primary_url,
    )


def _subject(run_date: date) -> str:
    return f"Podcast Tracker ({_format_date(run_date)})"


def _count_line(n_entries: int) -> str:
    label = "Episode" if n_entries == 1 else "Episodes"
    return f"{n_entries} {label}"


def render_failure(run_date: date, reason: str) -> RenderedDigest:
    """Render a 'discovery failed today' notification email.

    Used when ALL discovery surfaces fail so the user notices instead of getting
    a silent zero-match digest that hides the outage.
    """
    heading = f"Podcast Tracker ({_format_date(run_date)})"
    subheading = "Discovery failed"
    body_text = (
        f"{heading}\n"
        f"{subheading}\n\n"
        f"All discovery surfaces failed during today's run. The pipeline did not "
        f"process any episodes.\n\n"
        f"Reason: {reason}\n\n"
        f"State was not committed. The next scheduled run will retry; if the issue "
        f"persists, check the GitHub Actions logs.\n"
    )
    body_html = _env.get_template("digest.html.j2").render(
        contexts=[],
        heading=heading,
        subheading=subheading,
        failure_reason=reason,
    )
    body_html = _inliner.inline(body_html)
    return RenderedDigest(
        subject=f"Podcast Tracker ({_format_date(run_date)}) — discovery failed",
        html=body_html,
        text=body_text,
    )


def render(entries: list[DigestEntry], run_date: date) -> RenderedDigest:
    """Render the digest. Sorts entries by priority_score descending."""
    sorted_entries = sorted(
        entries,
        key=lambda e: e.episode.priority_score,
        reverse=True,
    )
    contexts = [
        _build_entry_context(entry, n=i + 1)
        for i, entry in enumerate(sorted_entries)
    ]
    heading = f"Podcast Tracker ({_format_date(run_date)})"
    subheading = _count_line(len(contexts))

    html_raw = _env.get_template("digest.html.j2").render(
        contexts=contexts, heading=heading, subheading=subheading,
    )
    html = _inliner.inline(html_raw)
    text = _env.get_template("digest.txt.j2").render(
        contexts=contexts, heading=heading, subheading=subheading,
    )
    return RenderedDigest(
        subject=_subject(run_date),
        html=html,
        text=text,
    )
