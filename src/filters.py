"""Haiku-based filter: drop "mention" and "unrelated" candidates so we only summarize real guest appearances."""
from __future__ import annotations

import logging
from pathlib import Path

import anthropic

from .models import EpisodeCandidate, Episode

logger = logging.getLogger(__name__)

FILTER_MODEL = "claude-haiku-4-5-20251001"
PROMPT_PATH = Path("prompts/filter.txt")
MAX_DESCRIPTION_CHARS = 1500

CLASSIFY_TOOL = {
    "name": "classify_candidates",
    "description": "Emit a classification for every input candidate.",
    "input_schema": {
        "type": "object",
        "properties": {
            "classifications": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "guid": {"type": "string"},
                        "classification": {
                            "type": "string",
                            "enum": ["guest", "mention", "unrelated"],
                        },
                        "confidence": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                        },
                    },
                    "required": ["guid", "classification", "confidence"],
                },
            }
        },
        "required": ["classifications"],
    },
}


def _candidates_xml(candidates: list[EpisodeCandidate]) -> str:
    """Wrap untrusted candidate metadata in XML. Truncate descriptions defensively."""
    parts = ["<untrusted_candidates>"]
    for c in candidates:
        desc = (c.description or "")[:MAX_DESCRIPTION_CHARS]
        parts.append(
            "  <candidate>\n"
            f"    <guid>{_xml_escape(c.guid)}</guid>\n"
            f"    <query>{_xml_escape(c.match_query)}</query>\n"
            f"    <match_type>{c.match_type}</match_type>\n"
            f"    <podcast>{_xml_escape(c.podcast)}</podcast>\n"
            f"    <title>{_xml_escape(c.title)}</title>\n"
            f"    <description>{_xml_escape(desc)}</description>\n"
            "  </candidate>"
        )
    parts.append("</untrusted_candidates>")
    return "\n".join(parts)


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _title_contains_query(candidate: EpisodeCandidate) -> bool:
    """Title-literal match: query appears in the title, case-insensitively.

    Restricted to named_person matches. Company names appear casually in titles
    ("OpenAI's CEO does X", "Sequoia bets on Y") without being the focus of the
    episode, so company-name candidates always go through the Haiku filter.
    """
    if candidate.match_type != "named_person":
        return False
    if not candidate.match_query:
        return False
    return candidate.match_query.lower() in (candidate.title or "").lower()


FILTER_BATCH_SIZE = 40  # ~40 candidates per Haiku call stays well under 30K input tokens


def classify(
    candidates: list[EpisodeCandidate],
    *,
    client: anthropic.Anthropic | None = None,
    keep: tuple[str, ...] = ("guest",),
) -> list[Episode]:
    """Classify candidates in batched Haiku calls. Return only those in `keep`.

    Three fast-paths skip Haiku entirely:
      1. Specific-podcast matches are trusted by definition.
      2. Title contains the literal query (very strong guest signal).
      3. After both, the remainder go to Haiku in chunks of FILTER_BATCH_SIZE.
    """
    bypass = [c for c in candidates if c.match_type == "specific_podcast"]
    remaining = [c for c in candidates if c.match_type != "specific_podcast"]

    title_match = [c for c in remaining if _title_contains_query(c)]
    needs_filter = [c for c in remaining if not _title_contains_query(c)]

    episodes: list[Episode] = []
    for c in bypass:
        episodes.append(Episode(candidate=c, filter_confidence=1.0, priority_score=0.0))
    for c in title_match:
        episodes.append(Episode(candidate=c, filter_confidence=0.95, priority_score=0.0))

    if not needs_filter:
        logger.info(
            "filter fast-paths consumed all candidates: bypassed=%d title_matched=%d",
            len(bypass), len(title_match),
        )
        return episodes

    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text()
    by_guid = {c.guid: c for c in needs_filter}
    kept_from_haiku = 0

    for batch_start in range(0, len(needs_filter), FILTER_BATCH_SIZE):
        batch = needs_filter[batch_start:batch_start + FILTER_BATCH_SIZE]
        user_message = _candidates_xml(batch)
        try:
            resp = client.messages.create(
                model=FILTER_MODEL,
                max_tokens=2048,
                temperature=0.0,
                system=system_prompt,
                tools=[CLASSIFY_TOOL],
                tool_choice={"type": "tool", "name": "classify_candidates"},
                messages=[{"role": "user", "content": user_message}],
            )
        except anthropic.APIError as e:
            logger.error("filter batch %d-%d call failed: %s",
                         batch_start, batch_start + len(batch), e)
            continue

        tool_use_block = next((b for b in resp.content if b.type == "tool_use"), None)
        if not tool_use_block:
            logger.error("filter batch %d returned no tool_use block; dropping this batch",
                         batch_start)
            continue

        for cls in tool_use_block.input.get("classifications", []):
            guid = cls.get("guid")
            if guid not in by_guid:
                continue
            if cls.get("classification") not in keep:
                continue
            episodes.append(Episode(
                candidate=by_guid[guid],
                filter_confidence=float(cls.get("confidence", 0.5)),
                priority_score=0.0,
            ))
            kept_from_haiku += 1

    logger.info(
        "filter complete: total=%d bypassed=%d title_matched=%d needs_filter=%d kept_by_haiku=%d -> total_kept=%d",
        len(candidates), len(bypass), len(title_match), len(needs_filter), kept_from_haiku,
        len(episodes),
    )
    return episodes


def assign_priority_scores(
    episodes: list[Episode],
    tier_weights: dict[str, int],
) -> None:
    """Compute the value-weighted priority score in-place.

    score = tier_weight + min(duration_min/30, 4) + filter_confidence * 5
    """
    for ep in episodes:
        tier = tier_weights.get(ep.candidate.match_type, 0)
        duration = min(ep.candidate.duration_minutes / 30, 4)
        ep.priority_score = tier + duration + ep.filter_confidence * 5
