# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Horizon is an AI news radar: it fetches items from many sources, routes each one to a *processing profile*, scores/filters/deduplicates it, enriches it with web-searched context, and renders a bilingual Markdown briefing published to GitHub Pages, email, webhooks, or MCP.

Python ≥ 3.11, managed with `uv`. No linter or formatter is configured in the repo.

## Commands

```bash
uv sync                      # runtime deps
uv sync --extra dev          # + pytest (dev is an optional extra, not a dep-group)
uv sync --extra openbb       # optional OpenBB financial source
uv sync --extra twitter      # optional Playwright Twitter mode

uv run pytest                            # full suite (addopts = -q, testpaths = tests)
uv run pytest tests/test_profiles.py     # one file
uv run pytest tests/test_profiles.py::test_name   # one test
uv run pytest -k "dedup"                 # by keyword
uv run pytest --cov=src                  # coverage (pytest-cov)

uv run horizon [--hours N] [-d DATA_DIR] [-c CONFIG] [-l LEVEL]   # main pipeline
uv run horizon-wizard        # interactive config generator → data/config.json
uv run horizon-mcp           # MCP stdio server
uv run horizon-webhook       # webhook test/send CLI
uv run horizon-discover      # search for new feeds matching discovery.topics
uv run python scripts/check_mcp.py       # MCP smoke check (no network AI calls)
```

Running the pipeline requires `data/config.json` (copy `data/config.example.json`) and a `.env` with the API keys named by `ai.api_key_env`.

## Architecture

### Pipeline (`src/orchestrator.py`)

`HorizonOrchestrator.run()` is the single spine. Each stage is a **public method that MCP re-enters independently** — keep them side-effect-free beyond the items they mutate:

1. `fetch_all_sources(since)` — every enabled scraper runs concurrently under one shared `httpx.AsyncClient`; per-source outcomes land in `FetchReport` (`last_fetch_report`). A failing scraper degrades the run; only *all* sources failing aborts it.
2. `merge_cross_source_duplicates()` — conservative URL-identity merge (`_deduplication_url_key` strips `utm_*`/tracking params, normalizes host/port/trailing slash). The requested profile is part of the key, so the same URL routed to different profiles stays separate.
3. `analyze_items()` — classification + scoring (see below).
4. `select_digest_items()` — threshold filter → per-profile AI topic dedup → Twitter reply expansion + re-analysis → **re-filter** (re-analysis can lower a score) → `apply_balanced_digest()` category quotas and `max_items`.
5. `enrich_items()` — second AI pass producing localized artifacts.
6. Summarize per language in `ai.languages`, save to `<data-dir>/summaries/`, copy to `docs/_posts/` with Jekyll front matter, then email + webhook.

Email bodies are rendered by `src/services/email_render.py` from Jinja2 templates resolved per file against `email.template_dir` (default `data/templates/email`) with a fallback to `src/_builtin_email_templates/` — so a digest can be restyled without a rebuild. It renders from the structured `ContentItem`s (via `DailySummarizer.build_view()`), never by re-parsing the Markdown; a broken template is logged and falls back rather than failing the run. Contract: `docs/email-templates.md`.

Items are mutated **in place** throughout; `ContentItem.processing` accumulates `classification` → `analysis` → `artifacts`.

### Processing profiles — the core extension point

A profile is a directory `profiles/<id>/` with `profile.json`, `match.md`, `analysis.md`, `enrichment.md`. Adding a content domain normally requires **no Python changes**. `ProfileRegistry` (`src/processing/profiles.py`) validates them at startup with `extra="forbid"` pydantic models; prompt paths may not escape the profile dir. Wheels ship `profiles/` as `src/_builtin_profiles` and fall back to it when the configured `profiles_dir` is absent.

Routing (`src/ai/classifier.py`): a source's `profile` field may be an explicit ID (skips AI), absent/`"auto"` (AI matches against every `match.md`), or a list (AI matches against that subset only). Classification failure falls back to `processing.default_profile`, or the first candidate if the default isn't a candidate. Changing an item's profile clears its stale analysis and artifacts.

**User preferences never live in profile files.** `threshold` and `topic_dedup` belong to `processing.profile_settings[<id>]` in config; profiles stay reusable and contributable. See `docs/profiles.md` for the full contract.

Enrichment (`src/ai/enricher.py`) is contract-driven: `profile.json` declares block IDs, which blocks are `optional`, which one is `primary` (rendered under the title without a heading), and per-block allowed `tools`. Tool planning runs once per item, then one artifact is generated per language. Generated output is validated against the block contract with one self-repair retry (`_complete_model` appends the validation error and re-asks). Source refs must trace back to actual tool results, or the artifact is rejected. `zh` output is normalized to Simplified Chinese (`src/ai/localization.py`).

Only `web_search` (DDGS) exists in `ToolRegistry` (`src/processing/tools.py`); a block may use it only if it declares it.

### AI clients (`src/ai/client.py`)

`create_ai_client(AIConfig)` returns a single client or, when `ai.provider_chain` is set, a `ChainedAIClient` that lazily builds providers and falls back on retryable errors (429/401/403/quota/502/503/empty response). Non-retryable errors propagate. Provider defaults (model, key env var, base URL) live in `AI_PROVIDER_DEFAULTS` in `src/models.py`; most non-Anthropic/Gemini providers reuse `OpenAIClient` with a different `base_url`. Token usage is accumulated globally in `src/ai/tokens.py`. `ai.decision` optionally adds a *decision model* (`src/ai/decisions.py`, TypeSafe Jev via OpenRouter's `/api/alpha/decisions`, not chat completions): typed `choice`/`score` answers, no text. It can take over profile classification and prefilter clear rejects before the main analysis; its questions are built from the same `match.md`/`analysis.md` (`src/ai/prompting/decisions.py`), and any failure falls back to the main model.

### Config

`src/models.py` holds every pydantic config model plus `SOURCE_REGISTRY`, which maps a `SourceType` to where its config lives and which nested fields produce items (used for profile-reference validation). `StorageManager.load_config()` expands `${VAR}` in **all** string leaves before validation, so any config value can reference an env var; unset vars round-trip literally so the error surfaces downstream instead of silently emptying.

Output paths go through `safe_output_path()`; URLs through `src/url_security.py`.

### MCP (`src/mcp/`)

`server.py` (FastMCP tool/resource surface) → `service.py` (staged orchestration, run persistence) → `horizon_adapter.py` (loads the Horizon package from a resolved repo path and calls the *same* orchestrator methods). **The MCP layer must not reimplement pipeline logic.** Runs persist per-stage JSON under `data/mcp-runs/<run_id>/` so a run can resume from `raw`/`scored`/`filtered`/`enriched`. Stdout is reserved for the MCP protocol — all human output goes to stderr via the shared Rich console.

### Source discovery (`src/discovery/`)

`horizon-discover` is a side pipeline, not a stage of `orchestrator.run()`: AI-generated
search queries → the shared `WebSearchTool` → feed resolution (`<link rel="alternate">`,
then common paths) → one AI scoring call per candidate → `docs/discovered-sources.md`.
`collect_existing_sources()` reads every configured source (disabled entries included) so
known subscriptions are dropped **before** any scoring call. It never writes to the config;
the report ends with a JSON snippet to paste into `sources.rss`. Contract: `docs/discovery.md`.

### Scrapers (`src/scrapers/`)

Subclass `BaseScraper`, implement `async fetch(since) -> List[ContentItem]`, build IDs with `_generate_id(source_type, subtype, native_id)`. Register the source in `SOURCE_REGISTRY`, `SourcesConfig`, and `fetch_all_sources()`. Sub-source labels for console breakdowns come from `metadata` keys in `_sub_source_label()`. Details and endpoints per scraper: `docs/scrapers.md`.

## Conventions

- Console output uses Rich on **stderr** (`Console(stderr=True)`), with icon lookups via `get_icons(config.display.icon_style)` — never hardcode emoji.
- AI JSON responses always go through `parse_json_response()` (`src/ai/utils.py`) and are validated with pydantic; on failure prefer a bounded self-repair retry, then a logged fallback, rather than raising into the pipeline.
- `tenacity` `@retry` wraps per-item AI calls; per-item failures are logged and skipped, not fatal.
- Tests are offline: `tests/conftest.py` only injects the repo root into `sys.path`; suites use fake AI clients and stubbed HTTP rather than network access. Keep new tests network-free.
- Profile contributions must pass `uv run pytest tests/test_profiles.py tests/test_prompting.py -q` (per `docs/profiles.md`).
- `data/config.github.json` is the config the (currently disabled) daily GitHub Actions workflow copies to `data/config.json`; keep secrets there as `${VAR}` references.
