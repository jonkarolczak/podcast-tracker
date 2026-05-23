# AGENTS.md

Project-specific guidance for AI agents working in this repo.

## Project purpose

See `README.md` for the user-facing overview and `spec/01_plan.md` for the canonical implementation plan.

## Build loop (per Jon's global convention)

- All planning files live in `/spec/` with numbered filenames: `00_brainstorm.md`, `01_plan.md`, `02_*.md`, etc.
- `progress.md` at the repo root is the running log of work completed.
- When implementing, follow the plan's phase structure. Do not skip the Phase 1 vertical slice.

## Code style

- Python 3.13. Type-annotate public functions.
- Prefer `httpx.AsyncClient` over `requests` for any I/O-bound external call.
- Use `pydantic` for config validation at load time; plain dataclasses elsewhere.
- Use `logging` with structured `extra={}` payloads; never `print()` outside CLI entrypoints.
- Pin exact versions in `requirements.txt`.
- Keep modules under ~200 lines. The 10-file split documented in the plan is the target structure.

## Security baseline (Phase 1 — non-negotiable)

- All untrusted text (transcript bodies, episode descriptions, feed titles) wraps in `<untrusted_*>` XML tags before reaching any LLM.
- All LLM calls use forced `tool_choice` for JSON output. No free-form output anywhere.
- yt-dlp invocations pass scheme/host/IP validation through `src/discovery.py::validate_url_for_ytdlp` before being invoked. Use the required flag set: `--max-filesize 500M --no-playlist --no-call-home --no-update --socket-timeout 30`.
- Workflow uses `permissions: contents: write` only.
- Secret-redaction filter registered on root logger at startup.
- Jinja2 templates use `select_autoescape(['html'])`; LLM-emitted URLs are NEVER rendered as links.
- All feed-derived strings sanitized (NFKC + control-char strip + length cap) before being written to state files committed to git history.

## Don'ts

- Don't add `youtube-transcript-api` — it's IP-blocked on GitHub Actions runners. Use `yt-dlp --write-auto-subs` instead.
- Don't use `byterm` for guest or company searches — it returns feeds, not episodes. Use `byperson` for both.
- Don't roll your own retry logic on the Anthropic SDK; it has production-grade retry built in.
- Don't add an idempotency-key header to Anthropic Messages calls — that header doesn't exist on the public API.
- Don't write a `pending_commit.json` partial-failure recovery mechanism. Accept that a crash between Resend 200 and git push means tomorrow re-emails. It's fine.
- Don't ship without VAD (`vad_filter=True`) in the faster-whisper call.
- Don't use `premailer` — unmaintained since 2021. Use `css-inline`.

## Running

```bash
python -m src.tracker --once <episode_url>   # process one episode end-to-end
python -m src.tracker --daily                # full discovery + digest (cron path)
```

## Testing

```bash
pytest                      # all tests
pytest tests/test_transcript.py -v   # one module
```

## Cron schedule

GitHub Actions cron at `5 11 * * *` and `5 12 * * *` UTC (6:05am Central year-round, handling DST). Idempotency guard in `state/last_run.txt` ensures one run per local day.

## Watchlist editing

`config/watchlist.yaml` is hand-edited. To add a new tracked person, append to the `people:` list. To add a company, append a `name:` entry under `companies:` with optional `aliases:` for variant spellings.
