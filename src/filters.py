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


def classify(
    candidates: list[EpisodeCandidate],
    *,
    client: anthropic.Anthropic | None = None,
    keep: tuple[str, ...] = ("guest",),
) -> list[Episode]:
    """Classify candidates in one batched Haiku call. Return only those in `keep`.

    Specific-podcast matches bypass the filter — they're trusted by definition.
    """
    bypass = [c for c in candidates if c.match_type == "specific_podcast"]
    needs_filter = [c for c in candidates if c.match_type != "specific_podcast"]

    episodes: list[Episode] = [
        Episode(candidate=c, filter_confidence=1.0, priority_score=0.0)
        for c in bypass
    ]

    if not needs_filter:
        return episodes

    client = client or anthropic.Anthropic()
    system_prompt = PROMPT_PATH.read_text()
    user_message = _candidates_xml(needs_filter)

    resp = client.messages.create(
        model=FILTER_MODEL,
        max_tokens=2048,
        temperature=0.0,
        system=system_prompt,
        tools=[CLASSIFY_TOOL],
        tool_choice={"type": "tool", "name": "classify_candidates"},
        messages=[{"role": "user", "content": user_message}],
    )

    tool_use_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_use_block:
        logger.error("filter call returned no tool_use block; dropping all non-bypass candidates")
        return episodes

    by_guid = {c.guid: c for c in needs_filter}
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

    logger.info(
        "filter complete",
        extra={
            "input_count": len(candidates),
            "bypassed": len(bypass),
            "needs_filter": len(needs_filter),
            "kept": len(episodes),
        },
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
