"""Content analysis using AI."""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import List, Mapping, Optional
from pydantic import ValidationError
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, MofNCompleteColumn
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

from .client import AIClient
from .classifier import ContentClassifier
from .decisions import DecisionClient
from .prompting.decisions import (
    SCORE_QUESTION,
    item_state,
    level_to_score,
    score_question,
)
from .prompting.analysis import analysis_system_prompt, analysis_user_prompt
from .utils import parse_json_response
from ..models import ContentAnalysis, ContentItem
from ..processing.content import select_content, split_content
from ..processing.profiles import ProfileRegistry

DEFAULT_THROTTLE_SEC = 0.0
EXCERPT_CHARS = 300


def source_excerpt(content: str, limit: int = EXCERPT_CHARS) -> str:
    """First ``limit`` characters of the content, cut on a word boundary.

    Stands in for the main model's summary when the decision model scores
    alone: topic deduplication and enrichment read it as a hint only.
    """
    text = " ".join((content or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"

class ContentAnalyzer:
    """Analyzes content items using AI to determine importance."""

    def __init__(
        self,
        ai_client: AIClient,
        profiles: ProfileRegistry,
        console: Optional[Console] = None,
        decision_client: Optional[DecisionClient] = None,
        profile_thresholds: Optional[Mapping[str, Optional[float]]] = None,
    ):
        self.client = ai_client
        self.profiles = profiles
        self.decision_client = decision_client
        self.profile_thresholds = dict(profile_thresholds or {})
        classification_client = (
            decision_client
            if decision_client is not None and decision_client.config.classification
            else None
        )
        self.classifier = ContentClassifier(
            ai_client, profiles, decision_client=classification_client
        )
        self.console = console or Console(stderr=True)

    @staticmethod
    def _parse_json_response(response: str) -> Optional[dict]:
        """Try multiple strategies to extract a JSON object from an AI response.

        Returns the parsed dict, or None if all strategies fail.
        """
        return parse_json_response(response)

    def _get_throttle_sec(self) -> float:
        """Return the configured inter-item throttle, clamped to zero or above."""
        config = getattr(self.client, "config", None)
        throttle_sec = getattr(config, "throttle_sec", DEFAULT_THROTTLE_SEC)
        return max(throttle_sec, 0.0)

    def _get_concurrency(self) -> int:
        """Return the configured analysis concurrency, clamped to 1 or above."""
        config = getattr(self.client, "config", None)
        concurrency = getattr(config, "analysis_concurrency", 1)
        return max(concurrency, 1)

    async def analyze_batch(self, items: List[ContentItem]) -> List[ContentItem]:
        throttle_sec = self._get_throttle_sec()
        concurrency = self._get_concurrency()
        semaphore = asyncio.Semaphore(concurrency)

        async def _process(item: ContentItem, index: int, progress_task) -> ContentItem:
            async with semaphore:
                try:
                    await self._analyze_item(item)
                except Exception as e:
                    logger.error("Error analyzing item %s: %s", item.id, e)
                    if item.processing:
                        item.processing.analysis = ContentAnalysis(
                            score=None,
                            reason="Analysis failed",
                            summary=item.title,
                        )
                if throttle_sec > 0 and index < len(items) - 1:
                    await asyncio.sleep(throttle_sec)
            progress.advance(progress_task)
            return item

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            MofNCompleteColumn(),
            transient=True,
            console=self.console,
        ) as progress:
            task = progress.add_task("Analyzing", total=len(items))
            coros = [
                _process(item, i, task) for i, item in enumerate(items)
            ]
            analyzed_items = await asyncio.gather(*coros)

        return analyzed_items

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=2, max=10)
    )
    async def _analyze_item(self, item: ContentItem) -> None:
        """Analyze a single content item.

        Args:
            item: Content item to analyze (modified in-place)
        """
        profile = await self.classifier.resolve(item)
        if item.processing:
            item.processing.artifacts.clear()

        content_parts = split_content(item.content)
        selected_content = select_content(
            content_parts.main,
            profile.definition.content.analysis_max_chars,
            profile.definition.content.sampling,
        )
        content_section = f"Content: {selected_content}" if selected_content else ""

        # Prepare discussion section (comments, engagement)
        discussion_parts = []
        if content_parts.comments:
            discussion_parts.append(
                f"Community Comments:\n{content_parts.comments[:1500]}"
            )

        meta = item.metadata
        engagement_items = []
        if meta.get("score"):
            engagement_items.append(f"score: {meta['score']}")
        if meta.get("descendants"):
            engagement_items.append(f"{meta['descendants']} comments")
        if meta.get("favorite_count"):
            engagement_items.append(f"{meta['favorite_count']} likes")
        if meta.get("retweet_count"):
            engagement_items.append(f"{meta['retweet_count']} retweets")
        if meta.get("reply_count"):
            engagement_items.append(f"{meta['reply_count']} replies")
        if meta.get("views"):
            engagement_items.append(f"{meta['views']} views")
        if meta.get("bookmarks"):
            engagement_items.append(f"{meta['bookmarks']} bookmarks")
        if meta.get("upvote_ratio"):
            engagement_items.append(f"upvote ratio: {meta['upvote_ratio']:.0%}")
        if engagement_items:
            discussion_parts.append(f"Engagement: {', '.join(engagement_items)}")
        if meta.get("discussion_url"):
            discussion_parts.append(f"Discussion: {meta['discussion_url']}")
        if meta.get("community_note"):
            discussion_parts.append(f"Community Note: {meta['community_note']}")

        discussion_section = "\n".join(discussion_parts) if discussion_parts else ""

        decision_score = await self._decision_score(
            item, profile, selected_content, discussion_section
        )
        if decision_score is not None and self._settle_with_decision(
            item, profile, decision_score, selected_content
        ):
            return

        # Generate user prompt
        user_prompt = analysis_user_prompt(item, content_section, discussion_section)

        # Get AI completion
        response = await self.client.complete(
            system=analysis_system_prompt(profile),
            user=user_prompt,
        )

        result, failure = self._validate_analysis_response(response)
        if result is None:
            repair_response = await self.client.complete(
                system=analysis_system_prompt(profile),
                user=(
                    user_prompt
                    + "\n\nYour previous response did not satisfy the output contract "
                    f"({failure}). Analyze the item again and return only the required JSON object."
                ),
                temperature=0,
            )
            result, failure = self._validate_analysis_response(repair_response)

        if result is None:
            logger.warning(
                "Could not parse analysis response for %s after one repair attempt (%s), using defaults",
                item.id,
                failure,
            )
            if item.processing:
                item.processing.analysis = ContentAnalysis(
                    score=None,
                    reason="Analysis response parse failed",
                    summary=item.title,
                )
            return

        if item.processing:
            result.decision_score = decision_score
            item.processing.analysis = result

    def _prefilter_cutoff(self, profile_id: str) -> Optional[float]:
        """Decision score under which the main model is not consulted, if any."""
        if self.decision_client is None or not self.decision_client.config.prefilter:
            return None
        threshold = self.profile_thresholds.get(profile_id)
        if threshold is None:
            return None
        return threshold - self.decision_client.config.prefilter_margin

    def _final_scoring(self) -> bool:
        return self.decision_client is not None and self.decision_client.config.final_scoring

    async def _decision_score(
        self,
        item: ContentItem,
        profile,
        content: str,
        discussion: str,
    ) -> Optional[float]:
        """The decision model's 1-10 score, or None when it is not used or fails."""
        if not self._final_scoring() and self._prefilter_cutoff(profile.id) is None:
            return None
        try:
            answers = await self.decision_client.decide(
                item_state(item, content, discussion),
                {SCORE_QUESTION: score_question(profile)},
            )
            score = answers[SCORE_QUESTION].score
        except Exception as exc:
            logger.warning(
                "Decision model could not score %s, using the main model: %s",
                item.id,
                exc,
            )
            return None
        if score is None:
            return None
        return round(level_to_score(score), 2)

    def _settle_with_decision(
        self,
        item: ContentItem,
        profile,
        score: float,
        content: str,
    ) -> bool:
        """Store a decision-only analysis; True when the main model is not needed.

        With ``final_scoring`` the decision score is authoritative for every
        item. As a prefilter it is final only for items clearly below the
        profile threshold: they never reach the digest, so the summary and tags
        the main model would write are not needed.
        """
        model = self.decision_client.config.model
        if self._final_scoring():
            analysis = ContentAnalysis(
                score=score,
                reason=f"Scored by the decision model ({model})",
                summary=source_excerpt(content) or item.title,
                decision_score=score,
                score_source="decision",
            )
        else:
            cutoff = self._prefilter_cutoff(profile.id)
            if cutoff is None or score >= cutoff:
                return False
            analysis = ContentAnalysis(
                score=score,
                reason=f"Rejected by the decision model ({model}) before full analysis",
                summary=item.title,
                decision_score=score,
                score_source="decision",
            )
        if item.processing:
            item.processing.analysis = analysis
        return True

    @classmethod
    def _validate_analysis_response(
        cls,
        response: str,
    ) -> tuple[Optional[ContentAnalysis], str]:
        parsed = cls._parse_json_response(response)
        if not isinstance(parsed, dict):
            return None, "response was not a JSON object"
        try:
            result = ContentAnalysis.model_validate(parsed)
        except ValidationError as exc:
            first_error = exc.errors(include_url=False)[0]
            location = ".".join(str(part) for part in first_error["loc"])
            return None, f"invalid field {location or '<root>'}: {first_error['type']}"
        if result.score is None:
            return None, "score is required by the analysis contract"
        return result, ""


@dataclass
class DecisionComparison:
    """How the decision model's scores compare with the main model's."""

    compared: int = 0
    mean_abs_gap: float = 0.0
    # Positive when the decision model scores higher on average.
    mean_bias: float = 0.0
    threshold_agreements: int = 0
    threshold_compared: int = 0
    top_n: int = 0
    top_overlap: int = 0
    # (title, decision score, main score), largest gaps first.
    largest_gaps: list[tuple[str, float, float]] = field(default_factory=list)


def compare_decision_scores(
    items: List[ContentItem],
    profile_thresholds: Mapping[str, Optional[float]],
    *,
    top_n: int = 20,
    gaps: int = 5,
) -> DecisionComparison:
    """Compare both scores on items the main model analyzed after the decision model.

    Items settled by the decision model alone carry no main score and are
    left out; with ``prefilter_margin`` at 10 every item is compared.
    """
    pairs = []
    for item in items:
        analysis = item.processing.analysis if item.processing else None
        if (
            analysis is None
            or analysis.score_source != "main"
            or analysis.score is None
            or analysis.decision_score is None
        ):
            continue
        pairs.append((item, analysis.decision_score, analysis.score))
    if not pairs:
        return DecisionComparison()

    result = DecisionComparison(compared=len(pairs))
    result.mean_abs_gap = sum(abs(d - m) for _, d, m in pairs) / len(pairs)
    result.mean_bias = sum(d - m for _, d, m in pairs) / len(pairs)
    for item, decision, main in pairs:
        threshold = profile_thresholds.get(item.processing.classification.profile)
        if threshold is None:
            continue
        result.threshold_compared += 1
        if (decision >= threshold) == (main >= threshold):
            result.threshold_agreements += 1
    result.top_n = min(top_n, len(pairs))
    by_decision = sorted(pairs, key=lambda pair: pair[1], reverse=True)[: result.top_n]
    by_main = sorted(pairs, key=lambda pair: pair[2], reverse=True)[: result.top_n]
    result.top_overlap = len(
        {id(pair[0]) for pair in by_decision} & {id(pair[0]) for pair in by_main}
    )
    result.largest_gaps = [
        (item.title, decision, main)
        for item, decision, main in sorted(
            pairs, key=lambda pair: abs(pair[1] - pair[2]), reverse=True
        )[:gaps]
        if decision != main
    ]
    return result
