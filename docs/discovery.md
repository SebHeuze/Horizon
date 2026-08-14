---
layout: default
title: Source Discovery
---

# Source Discovery

`horizon-discover` looks for feeds you are *not* subscribed to yet. It turns each
of your topics into web searches, resolves the results into RSS/Atom feeds,
scores them with one AI call each, and writes a Markdown report you can copy
entries from into `sources.rss`.

Nothing is added to your configuration automatically: the report is a
recommendation, the subscription stays your decision.

## Running it

```bash
uv run horizon-discover                       # topics from discovery.topics
uv run horizon-discover -t "RISC-V" -t "compilers"
uv run horizon-discover --force               # run even when discovery.enabled is false
uv run horizon-discover -o data/sources.md    # write the report elsewhere
uv run horizon-discover --no-webhook          # skip the notification
```

`-d/--data-dir`, `-c/--config` and `-l/--log-level` behave as in `horizon`.

The command exits without doing anything when `discovery.enabled` is `false`,
unless `--force` is passed — that is what keeps the weekly workflow inert until
you opt in.

## How topics are chosen

In order of precedence:

1. `--topic` flags (repeatable)
2. `discovery.topics` in the config
3. the distinct `category` values of your configured RSS feeds

If all three are empty the command stops with an error rather than guessing.

## What gets excluded

Every configured source is read before the search starts, and a candidate is
dropped as soon as it matches one of them:

| Source kind | Matched on |
|---|---|
| RSS | feed URL (host + path, `www.` and trailing slash normalized) **and** the whole feed host |
| Reddit | `reddit.com/r/<subreddit>` |
| GitHub | `github.com/<owner>/<repo>` |
| Twitter/X | `x.com/<user>`, `twitter.com/<user>` |
| Telegram | `t.me/<channel>` |

Disabled entries count as subscribed — you turned them off on purpose, so
re-recommending them would be noise. Social networks, video platforms and
aggregators (`youtube.com`, `linkedin.com`, `medium.com`, …) are skipped
outright: they are not feeds you can subscribe to through Horizon's RSS scraper.

The exclusion happens *before* the scoring call, so discovery never spends
tokens on a source you already follow.

## How a candidate becomes a recommendation

1. **Queries** — one AI call per topic produces `queries_per_topic` search
   queries. If the model returns unusable JSON, three fixed queries are used
   instead, so a bad response degrades the run rather than failing it.
2. **Search** — each query runs through the same `web_search` tool the
   enrichment blocks use (DDGS), asking for `search_results_per_query` results.
3. **Feed resolution** — the result URL is fetched (through the SSRF-safe
   request path used everywhere else in Horizon). If it is already a feed it is
   kept as is; otherwise `<link rel="alternate" type="application/rss+xml">` is
   followed, and failing that `/feed`, `/feed.xml`, `/rss`, `/rss.xml`,
   `/atom.xml` and `/index.xml` are probed.
4. **Scoring** — the feed is parsed with `feedparser`; its recent titles and its
   real publishing cadence (median gap between entry dates) go to one AI call
   that scores relevance, depth, regularity and authority, 0–2.5 each. A
   candidate the model cannot score is dropped, never guessed at.
5. **Filtering** — anything below `quality_threshold` is discarded, and at most
   `max_per_topic` sources are kept per topic. The same feed found under two
   topics is reported once.

Cost per run is roughly `topics × (1 + evaluated candidates)` AI calls;
`max_candidates_per_topic` is the hard cap on the second term.

## Configuration

```json
"discovery": {
  "enabled": false,
  "topics": ["AI research", "developer tooling"],
  "max_per_topic": 3,
  "queries_per_topic": 3,
  "search_results_per_query": 10,
  "max_candidates_per_topic": 12,
  "quality_threshold": 5.5,
  "request_timeout_sec": 15,
  "output_path": "docs/discovered-sources.md",
  "language": null
}
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Whether the command runs without `--force` |
| `topics` | `[]` | Interests to search for |
| `max_per_topic` | `3` | Recommendations kept per topic |
| `queries_per_topic` | `3` | Search queries generated per topic |
| `search_results_per_query` | `10` | Web results requested per query |
| `max_candidates_per_topic` | `12` | Upper bound on AI scoring calls per topic |
| `quality_threshold` | `5.5` | Minimum score to report a source (0–10) |
| `request_timeout_sec` | `15` | HTTP timeout when fetching pages and feeds |
| `output_path` | `docs/discovered-sources.md` | Where the report is written |
| `language` | `null` | Language of the AI-written reasons; defaults to the first `ai.languages` entry |

## The report

The report carries Jekyll front matter, so writing it under `docs/` publishes it
with the rest of the site. It groups sources by topic, and ends with a JSON
snippet of the strongest recommendations, shaped for `sources.rss`:

```json
[
  {
    "name": "The Chip Letter",
    "url": "https://example.com/feed.xml",
    "enabled": true,
    "category": "semiconductors"
  }
]
```

Paste the entries you want into `data/config.json` and adjust `category` so the
items land in the digest group you expect.

## Notification

When `webhook.enabled` is true, a summary of the run is sent through the
configured webhook with `message_kind` set to `discovery` — the top five
sources, their scores, reasons and feed URLs, plus the report path. Pass
`--no-webhook` to skip it.

## Weekly automation

`.github/workflows/weekly-discovery.yml.disabled` runs the command every Sunday
and commits the report. To enable it: rename the file without the `.disabled`
suffix, set `discovery.enabled` to `true` in `data/config.github.json`, and make
sure the secrets it maps match the provider in that config.
