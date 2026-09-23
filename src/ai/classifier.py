"""Resolve processing profiles for fetched content."""

import logging

from pydantic import BaseModel, Field, ValidationError

from .client import AIClient
from .decisions import DecisionClient
from .prompting.classification import (
    classification_system_prompt,
    classification_user_prompt,
)
from .prompting.decisions import (
    CLASSIFICATION_QUESTION,
    classification_question,
    item_state,
)
from .utils import parse_json_response
from ..models import ClassificationResult, ContentItem, ProcessingResult
from ..processing.profiles import LoadedProfile, ProfileRegistry

logger = logging.getLogger(__name__)


class ClassificationResponse(BaseModel):
    profile: str
    confidence: float = Field(ge=0, le=1)
    reason: str


class ContentClassifier:
    """Choose a profile from explicit source configuration or AI matching."""

    def __init__(
        self,
        client: AIClient,
        profiles: ProfileRegistry,
        decision_client: DecisionClient | None = None,
    ):
        self.client = client
        self.profiles = profiles
        self.decision_client = decision_client

    async def resolve(self, item: ContentItem) -> LoadedProfile:
        requested = item.profile or "auto"
        candidate_ids = (
            self._candidate_ids(requested) if isinstance(requested, list) else None
        )
        requested_id = requested.strip() if isinstance(requested, str) else None
        if (
            item.processing
            and item.processing.classification.method == "ai_match"
            and (
                requested_id == "auto"
                or requested_id == item.processing.classification.profile
                or (
                    candidate_ids is not None
                    and item.processing.classification.profile in candidate_ids
                )
            )
        ):
            return self.profiles.get(item.processing.classification.profile)
        if requested_id and requested_id != "auto":
            profile = self.profiles.get(requested_id)
            classification = ClassificationResult(
                profile=profile.id,
                method="source_override",
            )
            if item.processing is None:
                item.processing = ProcessingResult(classification=classification)
            else:
                profile_changed = item.processing.classification.profile != profile.id
                item.processing.classification = classification
                if profile_changed:
                    item.processing.analysis = None
                    item.processing.artifacts.clear()
            return profile

        try:
            result = await self._classify(item, candidate_ids)
            profile = self.profiles.get(result.profile)
            classification = ClassificationResult(
                profile=profile.id,
                method="ai_match",
                confidence=result.confidence,
                reason=result.reason,
            )
        except Exception as exc:
            fallback_id = (
                self.profiles.default_profile
                if candidate_ids is None
                or self.profiles.default_profile in candidate_ids
                else candidate_ids[0]
            )
            logger.warning(
                "Could not classify %s; using %s: %s",
                item.id,
                fallback_id,
                exc,
            )
            profile = self.profiles.get(fallback_id)
            classification = ClassificationResult(
                profile=profile.id,
                method="ai_match",
                confidence=0,
                reason=f"Classification failed; used fallback profile {profile.id}: {exc}",
            )

        item.processing = ProcessingResult(classification=classification)
        return profile

    def _candidate_ids(self, requested: list[str]) -> tuple[str, ...]:
        candidate_ids = tuple(profile_id.strip() for profile_id in requested)
        if not candidate_ids:
            raise ValueError("profile candidate list cannot be empty")
        if any(not profile_id for profile_id in candidate_ids):
            raise ValueError("profile candidates must be non-empty strings")
        if len(candidate_ids) != len(set(candidate_ids)):
            raise ValueError("profile candidates must be unique")
        for profile_id in candidate_ids:
            if profile_id == "auto":
                raise ValueError("profile candidate list cannot contain 'auto'")
            self.profiles.get(profile_id)
        return candidate_ids

    async def _classify(
        self,
        item: ContentItem,
        candidate_ids: tuple[str, ...] | None = None,
    ) -> ClassificationResponse:
        if self.decision_client is not None:
            try:
                return await self._classify_with_decision_model(item, candidate_ids)
            except Exception as exc:
                logger.warning(
                    "Decision model could not classify %s, using the main model: %s",
                    item.id,
                    exc,
                )
        response = await self.client.complete(
            system=classification_system_prompt(),
            user=classification_user_prompt(item, self.profiles, candidate_ids),
        )
        parsed = parse_json_response(response)
        if not isinstance(parsed, dict):
            raise ValueError("classifier did not return an object")
        try:
            result = ClassificationResponse.model_validate(parsed)
        except ValidationError as exc:
            raise ValueError("invalid classifier response") from exc
        allowed_ids = set(candidate_ids) if candidate_ids is not None else self.profiles.ids
        if result.profile not in allowed_ids:
            raise ValueError(
                f"classifier selected profile outside the allowed catalog: {result.profile}"
            )
        return result

    async def _classify_with_decision_model(
        self,
        item: ContentItem,
        candidate_ids: tuple[str, ...] | None,
    ) -> ClassificationResponse:
        profiles = (
            self.profiles.profiles
            if candidate_ids is None
            else tuple(self.profiles.get(profile_id) for profile_id in candidate_ids)
        )
        if len(profiles) == 1:
            return ClassificationResponse(
                profile=profiles[0].id,
                confidence=1,
                reason="Single candidate profile",
            )
        answers = await self.decision_client.decide(
            item_state(item, (item.content or "").strip()[:2000]),
            {CLASSIFICATION_QUESTION: classification_question(profiles)},
        )
        answer = answers[CLASSIFICATION_QUESTION]
        allowed_ids = {profile.id for profile in profiles}
        if answer.choice not in allowed_ids:
            raise ValueError(
                f"decision model selected profile outside the allowed catalog: {answer.choice}"
            )
        confidence = answer.probabilities.get(answer.choice, answer.confidence)
        confidence = 0.0 if confidence is None else min(max(confidence, 0.0), 1.0)
        return ClassificationResponse(
            profile=answer.choice,
            confidence=confidence,
            reason=f"Decision model ({self.decision_client.config.model})",
        )
