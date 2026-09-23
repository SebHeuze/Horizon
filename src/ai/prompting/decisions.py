"""State and questions sent to the decision model (see src/ai/decisions.py).

A decision model answers typed questions about a ``state``; it takes no system
prompt. The profile's own ``match.md`` and ``analysis.md`` therefore travel in
the question ``criteria`` and ``instructions``, so profiles stay the single
source of routing and scoring policy for both kinds of model.
"""

from typing import Any, Dict

from ...models import ContentItem
from ...processing.profiles import LoadedProfile
from .common import UNTRUSTED_INPUT_RULE

CLASSIFICATION_QUESTION = "profile"
SCORE_QUESTION = "importance"

# The Decisions API accepts at most 10 score levels, so the ladder covers
# 1-10: one level per point, two per band of the profile rubrics (the 0-2
# noise band starts at 1, which changes nothing for filtering). The returned
# position is 0-based and may be fractional; `level_to_score` maps it back.
SCORE_LEVELS = [
    "1 - noise: spam, off-topic, or purely promotional",
    "2 - noise: trivial update",
    "3 - low priority: routine or shallow",
    "4 - low priority",
    "5 - interesting but incremental",
    "6 - interesting and useful",
    "7 - high value, worth prompt attention",
    "8 - high value",
    "9 - exceptional",
    "10 - groundbreaking",
]
MAX_SCORE_LEVELS = 10


def level_to_score(position: float) -> float:
    """Map a 0-based position on SCORE_LEVELS to the 0-10 analysis scale."""
    return min(max(position, 0.0), len(SCORE_LEVELS) - 1) + 1


def item_state(
    item: ContentItem,
    content: str,
    discussion: str = "",
) -> Dict[str, Any]:
    state: Dict[str, Any] = {
        "title": item.title,
        "source_type": item.source_type.value,
        "author": item.author or "Unknown",
        "url": str(item.url),
        "content": content or "No excerpt available.",
    }
    if discussion:
        state["discussion"] = discussion
    return state


def classification_question(profiles: tuple[LoadedProfile, ...]) -> Dict[str, Any]:
    return {
        "type": "choice",
        "instructions": (
            "Route this content to exactly one processing profile. Base the "
            "decision on the content's form and purpose, not merely its topic. "
            f"{UNTRUSTED_INPUT_RULE}"
        ),
        "criteria": {
            profile.id: f"{profile.definition.name}. {profile.match_prompt}"
            for profile in profiles
        },
    }


def score_question(profile: LoadedProfile) -> Dict[str, Any]:
    return {
        "type": "score",
        "instructions": (
            "Rate the importance of this content on a 1-10 scale under the "
            "evaluation policy below. Base the rating only on the supplied item "
            f"and its metadata. {UNTRUSTED_INPUT_RULE}\n\n"
            f"{profile.analysis_prompt}"
        ),
        "criteria": SCORE_LEVELS,
    }
