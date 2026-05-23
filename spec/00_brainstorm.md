---
date: 2026-05-23
topic: podcast-tracker
---

# Podcast Tracker Agent

## Problem Frame

Jon tracks a set of companies as part of his job search and broader market intelligence work. Strong signal about company strategy, hiring posture, product direction, and culture surfaces in podcasts where employees — especially executives and senior ICs — appear as guests. Manually monitoring for new appearances across many companies and a handful of must-follow podcasts is high-overhead and easy to miss. He also wants the same coverage for a small set of specific podcasts he follows.

Goal: an automated agent that catches new podcast appearances by named people at watchlisted companies (and new episodes of specific watchlisted podcasts), produces a structured summary of the conversation, and delivers everything in a single daily digest email.

## Requirements

- **R1.** Maintain a user-provided watchlist with three independent layers: (a) companies (with name aliases for variants like SSI/Safe Superintelligence and Anysphere/Cursor), (b) named people to follow regardless of current employer, and (c) specific podcasts to follow regardless of guest. The initial watchlist contents are captured in the "Initial Watchlist" section below.
- **R2.** Daily, discover podcast episodes published in the last 24 hours that match any of: a named person from the watchlist appearing as guest, OR a watchlisted company name (or alias) in episode title/description, OR an episode from a watchlisted podcast feed. Person matches are name-based and do not require the person to currently work at any listed company.
- **R3.** For each matched episode, obtain a transcript via: (i) official transcript on the podcast website if published, (ii) YouTube auto-captions if the episode is on YouTube, (iii) Whisper.cpp transcription as fallback. Mark "transcript unavailable" when all three fail.
- **R4.** Generate a structured summary per episode capturing approximately 15 of the most important points raised in the conversation, plus episode metadata (title, guest, podcast, date, link to canonical source / transcript).
- **R5.** Deliver a single daily digest email at ~6am ET listing all new matched episodes from the prior 24 hours, with summaries inline and a link to the transcript or canonical source for each.
- **R6.** Track processed episodes durably so the same episode is never re-summarized or re-emailed.
- **R7.** Run automatically on a daily schedule (~6am ET) without requiring Jon's laptop to be on.

## Success Criteria

- A daily email arrives by ~8am ET on days when matches exist. On days with no matches, either an empty-digest email or no email (decision deferred to planning).
- Named-people matches have high precision; false positives stay under ~10%.
- No duplicate episode coverage across days.
- Transcription succeeds for ≥80% of matched episodes; the remainder are surfaced with a clear "transcript unavailable" note rather than dropped silently.
- Summaries are detailed enough that Jon rarely needs to listen to the original episode to extract the signal he cares about.

## Scope Boundaries

- **Not in scope (any version):** real-time/push notifications, Slack/Discord delivery, web UI for browsing past summaries, YouTube channels that aren't podcasts, clip-level navigation, non-English transcripts.
- **Not in scope (initial version):** "any employee" matching beyond named-people + company-name catch.
- **Not in scope (initial version):** historical backfill — coverage begins on launch date forward only.

## Key Decisions

- **Project home:** `~/projects/podcast-tracker/` as a new standalone project (not coupled to `job-research/`).
- **Watchlist input:** user-provided structured config file maintained in the repo; Jon will supply initial contents at planning/implementation kickoff.
- **Match scope:** named people + company-name catch in title/description (covers VIPs by name, catches lesser-known employees and "from Company X" phrasing).
- **Transcript strategy:** free sources first (official transcript → YouTube captions) → Whisper.cpp local transcription as fallback.
- **Runner:** GitHub Actions cron job. Free tier covers expected volume (2000 minutes/month on private repos) and removes the laptop-on dependency.
- **Delivery:** single daily digest email at ~6am ET.
- **Summary shape:** ~15 key points per episode plus metadata and link.

## Initial Watchlist

Provided by Jon on 2026-05-23. Format will be finalized during planning (YAML/JSON config file); this is the canonical content.

### Companies (with aliases where relevant)

- OpenAI
- Anthropic
- Google DeepMind *(alias: DeepMind)*
- xAI
- Meta AI
- Mistral AI *(alias: Mistral)*
- Cohere
- Safe Superintelligence *(alias: SSI)*
- Thinking Machines Lab *(alias: Thinking Machines)*
- NVIDIA
- CoreWeave
- Baseten
- Together AI *(alias: Together)*
- Modal
- Fireworks AI *(alias: Fireworks)*
- Groq
- Cerebras
- Anysphere *(alias: Cursor)*
- Cognition
- LangChain
- Glean
- Sierra
- Scale AI *(alias: Scale)*
- Coatue
- Altimeter Capital *(alias: Altimeter)*
- Atreides
- Andreessen Horowitz *(alias: a16z)*
- Sequoia Capital *(alias: Sequoia)*
- Founders Fund
- Khosla Ventures *(alias: Khosla)*

### Named People (matched by name regardless of current employer)

Operators / founders / researchers:

- Sam Altman
- Greg Brockman
- Jakub Pachocki
- Mark Chen
- Dario Amodei
- Daniela Amodei
- Jared Kaplan
- Jack Clark
- Jan Leike
- Andrej Karpathy
- Demis Hassabis
- Jeff Dean
- Elon Musk
- Yann LeCun
- Arthur Mensch
- Aidan Gomez
- Ilya Sutskever
- Mira Murati
- John Schulman
- Jensen Huang

Investors / analysts / commentators:

- Gavin Baker
- Brad Gerstner
- Jamin Ball
- Philippe Laffont
- Thomas Laffont
- Marc Andreessen
- Martin Casado
- David George
- Roelof Botha
- Pat Grady
- Sonya Huang
- Vinod Khosla
- Elad Gil
- Sarah Guo
- Dylan Patel
- Ben Thompson
- Doug O'Laughlin
- Nathan Labenz
- Dwarkesh Patel

### Specific Podcasts (every new episode summarized regardless of guest)

- Dwarkesh Podcast
- Invest Like the Best
- Everyday AI Podcast

### Notes on the watchlist

- Several listed people are no longer at the companies they're commonly associated with (e.g., Jan Leike, Ilya Sutskever, Mira Murati, Andrej Karpathy, John Schulman). Person-matching is intentionally name-based and does not depend on current employer.
- Many people on the list are investors, analysts, or journalists. They are still in scope: the agent surfaces any podcast appearance by any listed name.
- Dwarkesh Patel is both a tracked person and a tracked podcast host. The "specific podcast" rule will catch every Dwarkesh Podcast episode; the "person" rule will catch episodes where he appears as a guest on other shows.
- Company aliases above will be used as additional search strings to catch variant spellings in episode titles and descriptions.

## Dependencies / Assumptions

- Anthropic API key (already available in Jon's environment).
- A podcast discovery API key (Listen Notes or PodcastIndex — selection deferred to planning).
- A transactional email provider account (Resend, Postmark, or Gmail SMTP with app password — selection deferred to planning).
- A GitHub repository (public or private) to host the Actions cron job and code.
- Podcast volume per day stays modest enough that Whisper.cpp transcription on GitHub Actions CPU runners fits inside the free Actions minute budget. Cost guard behavior is a planning item.
- Jon supplies the initial watchlist (companies, named people, specific podcasts) before launch.

## Outstanding Questions

### Resolve Before Planning

(none — all product decisions are settled)

### Deferred to Planning

- [Affects R2] [Needs research] Pick discovery API: Listen Notes (stronger guest search, free tier limited, paid tiers if needed) vs. PodcastIndex (fully free, weaker guest search) vs. layered approach using Exa for fuzzy company-name catch.
- [Affects R3] [Technical] Whisper.cpp model size on GitHub Actions runners (base.en vs small.en vs medium.en) — pick the accuracy/runtime balance that fits within free-tier minutes for expected daily volume.
- [Affects R5] [Technical] Email provider — Resend (3000 free emails/month, simplest API) vs Postmark vs Gmail SMTP with app password.
- [Affects R5] [User decision deferred] On days with zero matches, send an empty-digest email confirming the run happened, or send nothing.
- [Affects R6] [Technical] State storage for "already seen" episodes — JSON file committed back by the Actions job vs SQLite committed back vs lightweight cloud KV store.
- [Affects R7] [Technical] Cost / volume guard behavior if matched episodes in a day would exceed the runner-time budget (e.g., cap on transcription attempts per run, prioritize specific-podcast feeds over fuzzy company catches, fall back to "transcript unavailable" gracefully).

## Next Steps

→ `/ce:plan` for structured implementation planning
