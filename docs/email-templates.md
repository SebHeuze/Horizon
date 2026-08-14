# Email templates

The daily email is rendered from [Jinja2](https://jinja.palletsprojects.com/) templates read from disk at run time. Editing them requires no reinstall, no rebuild, and no Python changes.

## How templates are resolved

Horizon looks for each template in `email.template_dir` (default `data/templates/email`) and falls back to the version bundled in the package. Resolution happens **per file**, so overriding the HTML body leaves the plain-text one untouched.

```
data/templates/email/
  summary.html.j2     ← yours, wins if present
  summary.txt.j2      ← optional

src/_builtin_email_templates/
  summary.html.j2     ← fallback, shipped in the wheel
  summary.txt.j2
```

To start from the built-in design:

```bash
mkdir -p data/templates/email
cp src/_builtin_email_templates/summary.html.j2 data/templates/email/
```

`data/` is the directory mounted as a volume in `docker-compose.yml`, so templates placed there survive image rebuilds and can be edited on a running deployment.

Point `email.html_template` / `email.text_template` elsewhere if you prefer different file names. Template names cannot escape `template_dir`: a path such as `../secret.txt` is rejected.

**Failures never block delivery.** A template that is missing or has a syntax error is logged and the built-in one is used instead; the email still goes out.

## Template context

Both templates receive the same variables.

### Top level

| Variable | Type | Description |
|---|---|---|
| `subject` | str | Rendered subject line |
| `date` | str | Digest date, `YYYY-MM-DD` |
| `language` | str | Language code of this edition (`en`, `zh`, …) |
| `sender_name` | str | `email.sender_name` |
| `unsubscribe_keyword` | str | `email.unsubscribe_keyword` |
| `total_fetched` | int | Items fetched before filtering |
| `selected_count` | int | Items in the digest |
| `stats_line` | str | Localized "From N items, M were selected" sentence |
| `summary_markdown` | str | Full digest as cleaned Markdown |
| `summary_html` | str | `summary_markdown` converted to HTML, pre-sanitized — insert with `\| safe` |
| `labels` | dict | Localized labels: `header`, `source`, `background`, `discussion`, `references`, `tags` |
| `theme` | dict | Colours (see below) |
| `groups` | list | Digest content grouped by profile — **empty when the pipeline could not supply structured items** |

Always guard the rich path with `{% if groups %}` and fall back to `{{ summary_html | safe }}`, as the built-in template does.

### `groups[]`

| Key | Description |
|---|---|
| `profile_id` | Processing profile ID, e.g. `tech-news` |
| `name` | Localized profile name |
| `entries` | The items in this group |

> The list is called `entries`, not `items`: in Jinja2, `group.items` would resolve to the built-in `dict.items` method.

### `groups[].entries[]`

| Key | Description |
|---|---|
| `index` | 1-based position within the group |
| `global_index` | 1-based position across the whole digest |
| `anchor_id` | Anchor used by the web digest, e.g. `item-tech-news-1` |
| `title` | Localized title |
| `url` | Article URL, or `None` when the source URL was unsafe |
| `score` | Analysis score (`0`–`10`), or `"?"` |
| `score_tier` | `high` (≥9), `good` (≥7), `mid` (≥5), `low` — pick a badge colour |
| `summary` | Analysis summary; only set when the item has no enrichment artifact |
| `primary_content` | Body of the profile's primary enrichment block |
| `blocks` | Secondary enrichment blocks: `id`, `title`, `content` |
| `sources` | Enrichment references: `title`, `url` (`None` if unsafe) |
| `tags` | Analysis tags, without the leading `#` |
| `source_line` | Pre-joined attribution, e.g. `rss · GitHub Changelog · Jul 30, 08:00` |
| `discussion_url` | Discussion link when it differs from `url`, else `None` |
| `source_type` | Raw source type, e.g. `rss` |
| `author` | Item author, may be `None` |
| `published_at` | `datetime`, may be `None` |

### `theme`

Defaults mirror the Horizon Dawn palette of the web digest (`docs/assets/css/horizon.css`). Override any subset via `email.theme` in `data/config.json`.

| Key | Default | Used for |
|---|---|---|
| `bg` | `#faf8f5` | Page background |
| `surface` | `#f3f0eb` | Table-of-contents panel |
| `border` | `#e0dbd3` | Borders and separators |
| `text` | `#2d2a3e` | Body text |
| `text_muted` | `#7c7891` | Attribution, footer |
| `link` | `#6d4aaa` | Links |
| `code_bg` | `#f0ede7` | Code background |
| `accent` | `#e0652e` | Source rule, tag pills |
| `header_from` / `header_via` / `header_to` | `#312e81` / `#be185d` / `#f97316` | Header gradient |
| `tier_high` / `tier_good` / `tier_mid` / `tier_low` | `#be185d` / `#e0652e` / `#d4a017` / `#7c7891` | Score badges |

## Writing for mail clients

The built-in template deliberately avoids things that break in Gmail, Outlook, and Apple Mail. Keep these constraints when you customize it:

- **Inline every style** in a `style="…"` attribute. External stylesheets, `<style>` blocks, and CSS custom properties are unreliable or stripped.
- **Lay out with tables.** `<table role="presentation">` is the only box model Outlook desktop renders consistently.
- **No `<details>` / `<summary>`.** Gmail removes them; references are rendered as a flat list instead.
- **No `@media (prefers-color-scheme)`.** One palette, driven by `theme`.
- **Quotes in CSS values get escaped.** Autoescaping turns `"` into `&#34;`; the built-in template uses an unquoted `font-family` identifier list to avoid it.
- **Give the gradient a fallback.** Outlook ignores `linear-gradient`, hence the `bgcolor` attribute on the header cell.

### Escaping

`summary.html.j2` is autoescaped; `summary.txt.j2` is not (it must emit no HTML). All item text is escaped on output, and URLs are validated in Python before they reach the template — an unsafe scheme such as `javascript:` arrives as `None`, so always guard links:

```jinja
{% if item.url %}<a href="{{ item.url }}">{{ item.title }}</a>{% else %}{{ item.title }}{% endif %}
```

Templates run in a Jinja2 sandbox, so attribute access into Python internals is blocked.

## Previewing a change

Templates are reloaded on every run, so the fastest loop is to render one straight from Python:

```python
from src.models import EmailConfig
from src.services.email_render import EmailRenderer, build_email_context

config = EmailConfig(imap_server="", smtp_server="", email_address="me@example.com")
context = build_email_context(config, "# Daily\n\nbody", "Subject", date="2026-07-30")
open("preview.html", "w", encoding="utf-8").write(EmailRenderer(config).render_html(context))
```

Pass `items=` and `summarizer=` to exercise the rich per-item rendering rather than the degraded Markdown path.
