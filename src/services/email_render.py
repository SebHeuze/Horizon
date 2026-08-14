"""Jinja2 rendering for the daily email summary.

Templates resolve against the user's ``email.template_dir`` first and fall
back, file by file, to the built-in ones shipped inside the package. Dropping
a single ``summary.html.j2`` into ``data/templates/email/`` therefore restyles
the digest without reinstalling anything.

This module owns template loading and context building; ``email.py`` keeps
SMTP/IMAP and MIME assembly.
"""

import html
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import markdown as markdown_lib
from jinja2 import ChoiceLoader, FileSystemLoader, TemplateError
from jinja2.sandbox import SandboxedEnvironment

from ..ai.markdown_utils import clean_app_summary_markdown
from ..ai.summarizer import (
    LABELS,
    DailySummarizer,
    localize_text,
    safe_http_url,
    source_parts,
)
from ..models import ContentItem, EmailConfig

logger = logging.getLogger(__name__)

BUILTIN_EMAIL_TEMPLATES_DIR = (
    Path(__file__).resolve().parents[1] / "_builtin_email_templates"
)

DEFAULT_HTML_TEMPLATE = "summary.html.j2"
DEFAULT_TEXT_TEMPLATE = "summary.txt.j2"

# Horizon Dawn palette, mirroring docs/assets/css/horizon.css. Mail clients get
# the light values only: no CSS variables, no prefers-color-scheme.
DEFAULT_THEME: Dict[str, str] = {
    "bg": "#faf8f5",
    "surface": "#f3f0eb",
    "border": "#e0dbd3",
    "text": "#2d2a3e",
    "text_muted": "#7c7891",
    "link": "#6d4aaa",
    "code_bg": "#f0ede7",
    "accent": "#e0652e",
    "header_from": "#312e81",
    "header_via": "#be185d",
    "header_to": "#f97316",
    "tier_high": "#be185d",
    "tier_good": "#e0652e",
    "tier_mid": "#d4a017",
    "tier_low": "#7c7891",
}

# Same thresholds as processScoreBadges() in docs/assets/js/horizon.js.
_SCORE_TIERS = ((9.0, "high"), (7.0, "good"), (5.0, "mid"))


def score_tier(score: Any) -> str:
    """Map a 0-10 score to a badge tier; unknown scores render as 'low'."""
    try:
        value = float(score)
    except (TypeError, ValueError):
        return "low"
    for threshold, tier in _SCORE_TIERS:
        if value >= threshold:
            return tier
    return "low"


def render_subject(config: EmailConfig, *, lang: str, date: str) -> str:
    """Render `email.subject_template`, falling back on an unknown placeholder."""
    default = EmailConfig.model_fields["subject_template"].default
    try:
        return config.subject_template.format(lang=lang, date=date)
    except (KeyError, IndexError, ValueError) as exc:
        logger.warning(
            "Invalid email.subject_template %r (%s); using the default",
            config.subject_template,
            exc,
        )
        return default.format(lang=lang, date=date)


def _item_context(view_item, language: str) -> Dict[str, Any]:
    """Flatten one SummaryItemView into template-facing plain data."""
    item: ContentItem = view_item.item
    artifact = item.processing.artifacts.get(language) if item.processing else None
    analysis = item.processing.analysis if item.processing else None

    primary_block = (
        next((block for block in artifact.blocks if block.primary), None)
        if artifact
        else None
    )
    # Mirrors _format_item: the raw analysis summary only shows without an artifact.
    summary = analysis.summary if analysis and not artifact else ""

    blocks = [
        {
            "id": block.id,
            "title": localize_text(block.title, language),
            "content": localize_text(block.content, language),
        }
        for block in (artifact.blocks if artifact else [])
        if not block.primary
    ]

    sources = []
    for source in artifact.sources if artifact else []:
        sources.append(
            {"title": source.title, "url": safe_http_url(source.url)}
        )

    discussion_url = safe_http_url(item.metadata.get("discussion_url") or "")
    if discussion_url == safe_http_url(item.url):
        discussion_url = None

    return {
        "index": view_item.index,
        "global_index": view_item.global_index,
        "anchor_id": view_item.anchor_id,
        "title": view_item.title,
        "url": safe_http_url(item.url),
        "score": view_item.score,
        "score_tier": score_tier(view_item.score),
        "summary": localize_text(summary, language),
        "primary_content": localize_text(
            primary_block.content if primary_block else "", language
        ),
        "blocks": blocks,
        "sources": sources,
        "tags": list(analysis.tags) if analysis and analysis.tags else [],
        "source_line": " · ".join(source_parts(item, language)),
        "discussion_url": discussion_url,
        "source_type": item.source_type.value,
        "author": item.author,
        "published_at": item.published_at,
    }


def build_email_context(
    config: EmailConfig,
    summary_md: str,
    subject: str,
    *,
    items: Optional[List[ContentItem]] = None,
    summarizer: Optional[DailySummarizer] = None,
    date: str = "",
    language: str = "en",
    total_fetched: Optional[int] = None,
) -> Dict[str, Any]:
    """Build the variables exposed to email templates.

    Without `items`/`summarizer` the context carries no `groups`, and the
    built-in template degrades to rendering `summary_html`.
    """
    cleaned = clean_app_summary_markdown(summary_md)
    summary_html = markdown_lib.markdown(html.escape(cleaned))

    labels = LABELS.get(language, LABELS["en"])

    groups: List[Dict[str, Any]] = []
    selected_count = len(items) if items else 0
    if items and summarizer:
        view = summarizer.build_view(items, language)
        selected_count = view.item_count
        for group in view.groups:
            groups.append(
                {
                    "profile_id": group.profile_id,
                    "name": group.name,
                    # Named "entries", not "items": Jinja2 attribute lookup on a
                    # dict resolves `group.items` to the built-in dict method.
                    "entries": [
                        _item_context(view_item, language)
                        for view_item in group.items
                    ],
                }
            )

    theme = dict(DEFAULT_THEME)
    theme.update(config.theme)

    return {
        "subject": subject,
        "date": date,
        "language": language,
        "sender_name": config.sender_name,
        "unsubscribe_keyword": config.unsubscribe_keyword,
        "total_fetched": total_fetched if total_fetched is not None else selected_count,
        "selected_count": selected_count,
        "summary_markdown": cleaned,
        "summary_html": summary_html,
        "stats_line": labels["selected_items"].format(
            total=total_fetched if total_fetched is not None else selected_count,
            selected=selected_count,
        ),
        "labels": labels,
        "theme": theme,
        "groups": groups,
    }


class EmailRenderer:
    """Renders the text and HTML bodies from overridable Jinja2 templates."""

    def __init__(self, config: EmailConfig, *, base_dir: Optional[Path] = None):
        self.config = config
        user_dir = Path(config.template_dir)
        if not user_dir.is_absolute():
            user_dir = (base_dir or Path.cwd()) / user_dir
        self.user_dir = user_dir

        self._env = self._make_env(
            ChoiceLoader(
                [
                    FileSystemLoader(str(user_dir)),
                    FileSystemLoader(str(BUILTIN_EMAIL_TEMPLATES_DIR)),
                ]
            )
        )
        self._builtin_env = self._make_env(
            FileSystemLoader(str(BUILTIN_EMAIL_TEMPLATES_DIR))
        )

    @staticmethod
    def _make_env(loader) -> SandboxedEnvironment:
        return SandboxedEnvironment(
            loader=loader,
            # ".html.j2" is escaped, ".txt.j2" is not.
            autoescape=lambda name: bool(name) and ".html" in name,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def _render(self, name: str, fallback_name: str, context: Dict[str, Any]) -> str:
        """Render `name`, falling back to the built-in template on any failure."""
        try:
            return self._env.get_template(name).render(**context)
        except TemplateError as exc:
            logger.error(
                "Email template %r failed (%s); falling back to the built-in %r",
                name,
                exc,
                fallback_name,
            )
        try:
            return self._builtin_env.get_template(fallback_name).render(**context)
        except TemplateError as exc:
            logger.error("Built-in email template %r failed: %s", fallback_name, exc)
            return ""

    def render_html(self, context: Dict[str, Any]) -> str:
        rendered = self._render(
            self.config.html_template, DEFAULT_HTML_TEMPLATE, context
        )
        if rendered.strip():
            return rendered
        # Last resort: never send an empty HTML part.
        return f"<pre>{html.escape(context.get('summary_markdown', ''))}</pre>"

    def render_text(self, context: Dict[str, Any]) -> str:
        rendered = self._render(
            self.config.text_template, DEFAULT_TEXT_TEMPLATE, context
        )
        return rendered if rendered.strip() else context.get("summary_markdown", "")
