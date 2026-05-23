"""Summarization with Claude Sonnet 4.6.

Key choices:
- Prompt-cached system block with 5-min TTL (all daily calls run in one burst).
- Forced tool_choice with strict JSON schema → no parse failures.
- AsyncAnthropic so multiple episodes summarize concurrently from tracker.py.
- Pre-flight count_tokens for the AnthropicBudget guard.
- Post-LLM heuristics: forbidden-phrase scan + URL-leak strip.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import anthropic

from .budget import AnthropicBudget, actual_cost_from_usage, estimate_sonnet_cost
from .models import Episode, Summary, SummaryPoint, Transcript

logger = logging.getLogger(__name__)

SUMMARIZE_MODEL = "claude-sonnet-4-6"
PROMPT_PATH = Path("prompts/summarize.txt")
MAX_TOKENS_OUTPUT = 4096
MAX_INPUT_TOKENS = 150_000
TRANSCRIPT_CHAR_CAP = 600_000  # rough char cap before token count check

FORBIDDEN_OPENERS = [
    re.compile(r"^the guest discussed\b", re.I),
    re.compile(r"^they (?:talked about|discussed)\b", re.I),
    re.compile(r"^an interesting conversation\b", re.I),
    re.compile(r"^a deep dive into\b", re.I),
]
URL_RE = re.compile(r"https?://\S+")


EMIT_SUMMARY_TOOL = {
    "name": "emit_summary",
    "description": "Emit the structured 15-point podcast summary.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {
                "type": "string",
                "description": "One sentence, max 20 words, naming the most important specific takeaway.",
            },
            "transcript_completeness": {
                "type": "string",
                "enum": ["complete", "partial", "low_quality"],
            },
            "bullets": {
                "type": "array",
                "minItems": 10,
                "maxItems": 18,
                "items": {
                    "type": "object",
                    "properties": {
                        "n": {"type": "integer"},
                        "category": {
                            "type": "string",
                            "enum": [
                                "STRATEGY", "HIRING", "MARKET", "TECHNICAL",
                                "QUOTE", "FINANCIAL", "PERSONNEL", "PRODUCT",
                            ],
                        },
                        "point": {"type": "string"},
                        "evidence": {"type": "string"},
                        "segment": {
                            "type": "string",
                            "enum": ["beginning", "middle", "end"],
                        },
                    },
                    "required": ["n", "category", "point", "segment"],
                },
            },
            "open_questions": {
                "type": "array",
                "maxItems": 3,
                "items": {"type": "string"},
            },
        },
        "required": ["headline", "transcript_completeness", "bullets"],
    },
}


def _system_blocks() -> list[dict]:
    prompt_text = PROMPT_PATH.read_text()
    return [
        {
            "type": "text",
            "text": prompt_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]


def _build_user_message(episode: Episode, transcript: Transcript) -> str:
    c = episode.candidate
    transcript_text = (transcript.text or "")[:TRANSCRIPT_CHAR_CAP]
    return (
        "<episode_metadata>\n"
        f"  <podcast>{_xml_escape(c.podcast)}</podcast>\n"
        f"  <episode_title>{_xml_escape(c.title)}</episode_title>\n"
        f"  <match_query>{_xml_escape(c.match_query)}</match_query>\n"
        f"  <published_date>{c.published_at.date().isoformat()}</published_date>\n"
        f"  <duration_minutes>{c.duration_minutes:.0f}</duration_minutes>\n"
        f"  <episode_url>{_xml_escape(c.episode_url)}</episode_url>\n"
        "</episode_metadata>\n\n"
        "<untrusted_transcript>\n"
        f"{transcript_text}\n"
        "</untrusted_transcript>\n\n"
        "Produce a structured ~15-point summary of this episode.\n\n"
        "Step 1 — Internal extraction:\n"
        "- Identify 20-25 candidate passages from the transcript: any moment with a specific claim,\n"
        "  named entity, number, decision, hiring signal, product detail, strategic framing,\n"
        "  contrarian view, confession, or quotable line.\n"
        "- For each candidate, note which third of the transcript it falls in.\n\n"
        "Step 2 — Draft ~15 bullets:\n"
        "- At least 3 bullets must come from each third (coverage spread).\n"
        "- Order by importance, not chronology.\n"
        "- Each bullet must contain at least one of: a named company, named person, specific number,\n"
        "  specific product or feature, direct decision, or verbatim short quote.\n"
        "- Assign exactly one category from STRATEGY / HIRING / MARKET / TECHNICAL / QUOTE /\n"
        "  FINANCIAL / PERSONNEL / PRODUCT.\n\n"
        "Step 3 — Self-revision:\n"
        "- If any two bullets cover the same claim in different words, replace the weaker one.\n"
        "- If any bullet starts with \"The guest discussed\", \"They talked about\", \"An interesting\n"
        "  conversation\", or \"A deep dive into\", rewrite it to lead with the specific claim.\n"
        "- If any bullet contains a number or named entity you cannot locate in the transcript, remove it.\n\n"
        "Hard rules:\n"
        "- No speculation about implications. Report what was said.\n"
        "- No filler adverbs: \"quietly\", \"deeply\", \"fundamentally\", \"remarkably\", \"arguably\",\n"
        "  \"notably\", \"interestingly\".\n"
        "- No \"It's not X — it's Y\" framing.\n"
        "- Maximum 35 words per bullet.\n"
        "- Direct quotes use straight double-quotes and must be verbatim.\n"
        "- If the transcript is incomplete, produce fewer bullets rather than padding.\n"
    )


def _xml_escape(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _clean_bullet(point: str) -> str:
    """Strip URLs (LLM-injected links never belong in the digest) and trim."""
    return URL_RE.sub("", point).strip()


def _post_filter_summary(summary: Summary) -> Summary:
    """Strip URLs, drop bullets matching forbidden openers, renumber."""
    cleaned: list[SummaryPoint] = []
    n = 1
    for b in summary.bullets:
        cleaned_point = _clean_bullet(b.point)
        if not cleaned_point:
            continue
        if any(p.match(cleaned_point) for p in FORBIDDEN_OPENERS):
            logger.info("dropped bullet with forbidden opener: %r", cleaned_point[:80])
            continue
        cleaned.append(SummaryPoint(
            n=n,
            category=b.category,
            point=cleaned_point,
            evidence=_clean_bullet(b.evidence),
            segment=b.segment,
        ))
        n += 1
    return Summary(
        headline=_clean_bullet(summary.headline),
        bullets=cleaned,
        open_questions=[_clean_bullet(q) for q in summary.open_questions if _clean_bullet(q)],
        transcript_completeness=summary.transcript_completeness,
    )


def _summary_from_tool_input(payload: dict) -> Summary:
    bullets = [
        SummaryPoint(
            n=b.get("n", i + 1),
            category=b.get("category", "STRATEGY"),
            point=b.get("point", ""),
            evidence=b.get("evidence", ""),
            segment=b.get("segment", "middle"),
        )
        for i, b in enumerate(payload.get("bullets", []))
    ]
    return Summary(
        headline=payload.get("headline", ""),
        bullets=bullets,
        open_questions=payload.get("open_questions", []) or [],
        transcript_completeness=payload.get("transcript_completeness", "complete"),
    )


async def summarize(
    episode: Episode,
    transcript: Transcript,
    *,
    budget: AnthropicBudget,
    client: anthropic.AsyncAnthropic | None = None,
) -> Summary | None:
    """Summarize one episode. Returns None if budget exhausted or call fails."""
    if not transcript.text or not transcript.text.strip():
        return None

    client = client or anthropic.AsyncAnthropic(max_retries=5)
    system_blocks = _system_blocks()
    user_message = _build_user_message(episode, transcript)

    # Pre-flight: estimate input tokens via count_tokens
    try:
        count = await client.messages.count_tokens(
            model=SUMMARIZE_MODEL,
            system=system_blocks,
            tools=[EMIT_SUMMARY_TOOL],
            messages=[{"role": "user", "content": user_message}],
        )
        input_tokens = count.input_tokens
    except Exception as e:
        logger.warning("count_tokens failed for %s; using char heuristic: %s", episode.candidate.guid, e)
        # ~4 chars per token rule of thumb
        input_tokens = len(user_message) // 4

    if input_tokens > MAX_INPUT_TOKENS:
        logger.warning(
            "transcript too long for %s: %d tokens > %d cap",
            episode.candidate.guid, input_tokens, MAX_INPUT_TOKENS,
        )
        return None

    estimated_cost = estimate_sonnet_cost(input_tokens)
    if not budget.can_afford(estimated_cost):
        logger.warning(
            "anthropic budget exhausted; skipping summarize for %s (est $%.4f, spent $%.4f)",
            episode.candidate.guid, estimated_cost, budget.spent_usd,
        )
        return None

    try:
        async with client.messages.stream(
            model=SUMMARIZE_MODEL,
            max_tokens=MAX_TOKENS_OUTPUT,
            temperature=0.3,
            system=system_blocks,
            tools=[EMIT_SUMMARY_TOOL],
            tool_choice={"type": "tool", "name": "emit_summary"},
            messages=[{"role": "user", "content": user_message}],
        ) as stream:
            final = await stream.get_final_message()
    except anthropic.APIError as e:
        logger.error("summarize API call failed for %s: %s", episode.candidate.guid, e)
        return None

    budget.spend(actual_cost_from_usage(final.usage))

    tool_block = next((b for b in final.content if b.type == "tool_use"), None)
    if not tool_block:
        logger.error("summarize returned no tool_use block for %s", episode.candidate.guid)
        return None

    raw_summary = _summary_from_tool_input(tool_block.input)
    summary = _post_filter_summary(raw_summary)
    logger.info(
        "summarize complete",
        extra={
            "guid": episode.candidate.guid,
            "bullets": len(summary.bullets),
            "completeness": summary.transcript_completeness,
        },
    )
    return summary
