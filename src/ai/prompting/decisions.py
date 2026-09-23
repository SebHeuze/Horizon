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

# One level per point of the 0-10 analysis scale, so the returned position
# (0-based, possibly fractional) is directly the score. The profile rubric in
# the instructions says what each band means for that domain.
SCORE_LEVELS = [
    "0 - noise: spam, off-topic, or purely promotional",
    "1 - noise",
    "2 - noise: trivial update",
    "3 - low priority: routine or shallow",
    "4 - low priority",
    "5 - interesting but incremental",
    "6 - interesting and useful",
    "7 - high value, worth prompt attention",
    "8 - high value",
    "9 - exceptional, groundbreaking",
    "10 - exceptional, a landmark",
]


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
            "Rate the importance of this content on a 0-10 scale under the "
            "evaluation policy below. Base the rating only on the supplied item "
            f"and its metadata. {UNTRUSTED_INPUT_RULE}\n\n"
            f"{profile.analysis_prompt}"
        ),
        "criteria": SCORE_LEVELS,
    }
