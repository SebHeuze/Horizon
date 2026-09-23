"""Client for structured decision models (TypeSafe Jev via OpenRouter).

A decision model does not generate text. It receives an application ``state``
plus typed ``questions`` and returns one typed answer per question:

- ``choice``: the winning option key, per-option probabilities, a confidence;
- ``score``: a (possibly fractional) position on an ordered rubric, 0-based;
- ``noul``: the probability that the answer is "yes".

It is called through OpenRouter's Decisions endpoint, which is separate from
the chat completions API used by :mod:`src.ai.client`.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..models import DecisionConfig
from .tokens import record_usage

logger = logging.getLogger(__name__)

USAGE_PROVIDER = "decision"


class DecisionError(RuntimeError):
    """The decision model could not answer; callers fall back to the main model."""


class DecisionAnswer(BaseModel):
    """One typed answer. Only the fields of its ``type`` are populated."""

    model_config = ConfigDict(extra="allow")

    type: Literal["choice", "score", "noul"]
    choice: Optional[str] = None
    score: Optional[float] = Field(default=None, allow_inf_nan=False)
    noul: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    confidence: Optional[float] = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    probabilities: Dict[str, float] = Field(default_factory=dict)


class DecisionResponse(BaseModel):
    model_config = ConfigDict(extra="allow")

    answers: Dict[str, DecisionAnswer]
    usage: Dict[str, Any] = Field(default_factory=dict)


class DecisionClient:
    """Thin async client for the Decisions endpoint."""

    def __init__(
        self,
        config: DecisionConfig,
        *,
        transport: Optional[httpx.AsyncBaseTransport] = None,
    ):
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise ValueError(
                f"Missing API key for ai.decision: environment variable "
                f"{config.api_key_env} is not set"
            )
        self.config = config
        self._api_key = api_key
        self._transport = transport

    async def decide(
        self,
        state: Any,
        questions: Dict[str, Dict[str, Any]],
    ) -> Dict[str, DecisionAnswer]:
        """Ask every question about ``state``; raise DecisionError on any failure."""
        payload = {
            "model": self.config.model,
            "state": state,
            "questions": questions,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.config.timeout_sec,
                transport=self._transport,
            ) as client:
                response = await client.post(
                    self.config.base_url,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.HTTPError as exc:
            raise DecisionError(f"decision request failed: {exc}") from exc

        if response.status_code != 200:
            raise DecisionError(
                f"decision request returned HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise DecisionError("decision response is not JSON") from exc
        if isinstance(body, dict) and body.get("error"):
            raise DecisionError(f"decision API error: {body['error']}")
        try:
            parsed = DecisionResponse.model_validate(body)
        except ValidationError as exc:
            raise DecisionError("unexpected decision response shape") from exc

        record_usage(
            USAGE_PROVIDER,
            input_tokens=_as_int(parsed.usage.get("input_tokens")),
            output_tokens=_as_int(parsed.usage.get("output_tokens")),
        )
        missing = set(questions) - set(parsed.answers)
        if missing:
            raise DecisionError(
                f"decision response lacks answers for: {', '.join(sorted(missing))}"
            )
        for name, answer in parsed.answers.items():
            expected = questions.get(name, {}).get("type")
            if expected and answer.type != expected:
                raise DecisionError(
                    f"decision answer {name} has type {answer.type}, expected {expected}"
                )
        return parsed.answers


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def create_decision_client(config: Optional[DecisionConfig]) -> Optional[DecisionClient]:
    """Build the decision client, or None when none is configured or usable.

    A missing key disables the decision model with a warning instead of
    failing the run: every decision step has a main-model fallback.
    """
    if config is None or not (config.classification or config.prefilter):
        return None
    try:
        return DecisionClient(config)
    except ValueError as exc:
        logger.warning("Decision model disabled: %s", exc)
        return None
