"""Markdown reporting for discovered sources."""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Dict, List, Sequence

from .._file_utils import _atomic_write_text
from .discoverer import SourceRecommendation

# Score at which a recommendation is presented as a straight "subscribe".
STRONG_RECOMMENDATION_SCORE = 8.0


class DiscoveryReporter:
    """Render and persist the discovery report.

    The report is written under ``docs/`` by default, so it is published by the
    same GitHub Pages job as the digests; the Jekyll front matter is included
    for that reason.
    """

    def __init__(self, *, title: str = "Discovered Sources"):
        self.title = title

    def generate_report(
        self,
        recommendations: Sequence[SourceRecommendation],
        topics: Sequence[str],
        *,
        date: str | None = None,
        skipped_existing: int = 0,
    ) -> str:
        """Return the full Markdown report."""
        date = date or datetime.now().strftime("%Y-%m-%d")
        sections = [
            self._front_matter(),
            self._header(topics, date, len(recommendations), skipped_existing),
        ]

        if not recommendations:
            sections.append(
                "No new source cleared the quality threshold this time. "
                "Widen `discovery.topics`, lower `discovery.quality_threshold`, "
                "or raise `discovery.search_results_per_query` and run it again.\n"
            )
            return "\n".join(sections)

        grouped = self._group_by_topic(recommendations)
        for topic in topics:
            sources = grouped.get(topic)
            if sources:
                sections.append(self._topic_section(topic, sources))

        sections.append(self._config_snippet(recommendations))
        return "\n".join(sections)

    def save_report(self, report: str, output_path: str | Path) -> Path:
        """Write the report atomically, creating parent directories."""
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, report)
        return path

    def _front_matter(self) -> str:
        return f"---\nlayout: default\ntitle: {self.title}\n---\n"

    def _header(
        self,
        topics: Sequence[str],
        date: str,
        found: int,
        skipped_existing: int,
    ) -> str:
        return (
            f"# {self.title}\n\n"
            f"- **Date**: {date}\n"
            f"- **Topics**: {', '.join(topics) if topics else '—'}\n"
            f"- **New sources found**: {found}\n"
            f"- **Already subscribed (excluded)**: {skipped_existing}\n"
        )

    def _group_by_topic(
        self, recommendations: Sequence[SourceRecommendation]
    ) -> Dict[str, List[SourceRecommendation]]:
        grouped: Dict[str, List[SourceRecommendation]] = {}
        for recommendation in recommendations:
            grouped.setdefault(recommendation.topic, []).append(recommendation)
        return grouped

    def _topic_section(self, topic: str, sources: Sequence[SourceRecommendation]) -> str:
        lines = [f"\n## {topic}\n"]
        for index, source in enumerate(sources, 1):
            verdict = (
                "subscribe"
                if source.quality_score >= STRONG_RECOMMENDATION_SCORE
                else "worth a look"
            )
            lines.append(f"### {index}. {source.name} — {source.quality_score:.1f}/10\n")
            lines.append(f"- **Feed**: `{source.feed_url}`")
            lines.append(f"- **Site**: {source.site_url}")
            lines.append(f"- **Updates**: {source.update_frequency}")
            lines.append(f"- **Verdict**: {verdict}")
            lines.append(f"- **Why**: {source.reason}")
            if source.recent_posts:
                lines.append("- **Recent posts**:")
                lines.extend(f"  - {post}" for post in source.recent_posts[:3])
            lines.append("")
        return "\n".join(lines)

    def _config_snippet(self, recommendations: Sequence[SourceRecommendation]) -> str:
        strong = [
            recommendation
            for recommendation in recommendations
            if recommendation.quality_score >= STRONG_RECOMMENDATION_SCORE
        ]
        selected = strong or list(recommendations)
        entries = json.dumps(
            [recommendation.to_rss_config() for recommendation in selected],
            indent=2,
            ensure_ascii=False,
        )
        return (
            "\n## Adding these to your config\n\n"
            "Append the entries below to `sources.rss` in `data/config.json`, "
            "then adjust `category` so the item lands in the digest group you want.\n\n"
            f"```json\n{entries}\n```\n"
        )
