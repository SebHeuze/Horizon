import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.ai.analyzer import ContentAnalyzer
from src.ai.classifier import ContentClassifier
from src.ai.decisions import (
    DecisionAnswer,
    DecisionClient,
    DecisionError,
    create_decision_client,
)
from src.ai.prompting.decisions import SCORE_LEVELS
from src.models import AIConfig, ContentItem, DecisionConfig, SourceType
from src.processing import ProfileRegistry


PROFILES = ProfileRegistry.load(
    Path(__file__).resolve().parents[1] / "profiles", "tech-news"
)
ANALYSIS_JSON = '{"score": 8, "reason": "Solid", "summary": "Main model summary", "tags": ["ai"]}'


def _item(profile="tech-news") -> ContentItem:
    return ContentItem(
        id="rss:test:1",
        source_type=SourceType.RSS,
        title="A new compiler release",
        url="https://example.com/item",
        content="Release notes for a compiler.",
        published_at=datetime(2026, 9, 1, tzinfo=timezone.utc),
        profile=profile,
    )


class FakeDecisionClient:
    def __init__(self, answers=None, error=None, **config):
        self.config = DecisionConfig(**config)
        self.answers = answers or {}
        self.error = error
        self.calls = []

    async def decide(self, state, questions):
        self.calls.append((state, questions))
        if self.error:
            raise self.error
        return {name: DecisionAnswer(**answer) for name, answer in self.answers.items()}


def _main_model(calls):
    async def complete(**kwargs):
        calls.append(kwargs)
        return ANALYSIS_JSON

    return SimpleNamespace(complete=complete)


# --- client ---------------------------------------------------------------


def _client(handler, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    return DecisionClient(DecisionConfig(), transport=httpx.MockTransport(handler))


def test_client_posts_state_and_questions(monkeypatch):
    seen = {}

    def handler(request):
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "typesafe/jev-1.13",
                "answers": {
                    "team": {
                        "type": "choice",
                        "choice": "billing",
                        "confidence": 0.9,
                        "probabilities": {"billing": 0.9, "sales": 0.1},
                    }
                },
                "usage": {"input_tokens": 12, "output_tokens": 3, "cost": 0.00001},
            },
        )

    client = _client(handler, monkeypatch)
    questions = {"team": {"type": "choice", "instructions": "?", "criteria": {"billing": "b", "sales": "s"}}}
    answers = asyncio.run(client.decide({"ticket": "refund"}, questions))

    assert seen["auth"] == "Bearer test-key"
    assert seen["body"] == {
        "model": "~typesafe/jev-latest",
        "state": {"ticket": "refund"},
        "questions": questions,
    }
    assert answers["team"].choice == "billing"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(429, json={"error": {"message": "rate limited"}}),
        httpx.Response(200, json={"error": {"message": "bad question"}}),
        httpx.Response(200, json={"answers": {}}),
        httpx.Response(200, json={"answers": {"q": {"type": "noul", "noul": 0.5}}}),
        httpx.Response(200, text="not json"),
    ],
)
def test_client_raises_decision_error_on_unusable_response(monkeypatch, response):
    client = _client(lambda request: response, monkeypatch)
    with pytest.raises(DecisionError):
        asyncio.run(client.decide("state", {"q": {"type": "score", "criteria": ["a", "b"]}}))


def test_factory_skips_missing_key_or_disabled_steps(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert create_decision_client(None) is None
    assert create_decision_client(DecisionConfig()) is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    assert create_decision_client(DecisionConfig(classification=False)) is None
    assert create_decision_client(DecisionConfig()) is not None


def test_ai_config_accepts_decision_block():
    config = AIConfig.model_validate(
        {
            "provider": "openai",
            "model": "m",
            "api_key_env": "OPENROUTER_API_KEY",
            "decision": {"prefilter": True, "prefilter_margin": 1.5},
        }
    )
    assert config.decision.model == "~typesafe/jev-latest"
    assert config.decision.classification is True
    assert config.decision.prefilter_margin == 1.5


# --- classification -------------------------------------------------------


def test_decision_model_routes_candidate_list():
    decision = FakeDecisionClient(
        answers={
            "profile": {
                "type": "choice",
                "choice": "tech-blog",
                "confidence": 0.8,
                "probabilities": {"tech-news": 0.2, "tech-blog": 0.8},
            }
        }
    )
    main_calls = []
    item = _item(profile=["tech-news", "tech-blog"])

    profile = asyncio.run(
        ContentClassifier(_main_model(main_calls), PROFILES, decision).resolve(item)
    )

    assert profile.id == "tech-blog"
    assert main_calls == []
    assert item.processing.classification.method == "ai_match"
    assert item.processing.classification.confidence == 0.8
    state, questions = decision.calls[0]
    assert state["title"] == "A new compiler release"
    assert set(questions["profile"]["criteria"]) == {"tech-news", "tech-blog"}


def test_decision_choice_outside_candidates_falls_back_to_main_model():
    decision = FakeDecisionClient(
        answers={"profile": {"type": "choice", "choice": "finance-news"}}
    )
    calls = []

    async def complete(**kwargs):
        calls.append(kwargs)
        return '{"profile":"tech-news","confidence":0.7,"reason":"News"}'

    item = _item(profile=["tech-news", "tech-blog"])
    profile = asyncio.run(
        ContentClassifier(SimpleNamespace(complete=complete), PROFILES, decision).resolve(item)
    )

    assert profile.id == "tech-news"
    assert len(calls) == 1


# --- scoring prefilter ----------------------------------------------------


def _analyze(decision, thresholds, item=None):
    calls = []
    item = item or _item()
    analyzer = ContentAnalyzer(
        _main_model(calls),
        PROFILES,
        decision_client=decision,
        profile_thresholds=thresholds,
    )
    asyncio.run(analyzer._analyze_item(item))
    return item, calls


def test_prefilter_rejects_clear_misses_without_main_model():
    decision = FakeDecisionClient(
        answers={"importance": {"type": "score", "score": 2.4}}, prefilter=True
    )

    item, calls = _analyze(decision, {"tech-news": 6.0})

    assert calls == []
    assert item.processing.analysis.score == 2.4
    assert item.processing.analysis.summary == item.title
    _, questions = decision.calls[0]
    assert questions["importance"]["criteria"] == SCORE_LEVELS
    assert "Scoring rubric" in questions["importance"]["instructions"]


def test_prefilter_margin_keeps_borderline_items_for_main_model():
    decision = FakeDecisionClient(
        answers={"importance": {"type": "score", "score": 5.2}}, prefilter=True
    )

    item, calls = _analyze(decision, {"tech-news": 6.0})

    assert len(calls) == 1
    assert item.processing.analysis.score == 8
    assert item.processing.analysis.summary == "Main model summary"


@pytest.mark.parametrize(
    "decision, thresholds",
    [
        (FakeDecisionClient(error=DecisionError("down"), prefilter=True), {"tech-news": 6.0}),
        (FakeDecisionClient(answers={"importance": {"type": "score", "score": 0}}, prefilter=True), {}),
        (FakeDecisionClient(answers={"importance": {"type": "score", "score": 0}}), {"tech-news": 6.0}),
    ],
    ids=["decision-failure", "no-threshold", "prefilter-disabled"],
)
def test_main_model_analyzes_when_prefilter_cannot_decide(decision, thresholds):
    item, calls = _analyze(decision, thresholds)

    assert len(calls) == 1
    assert item.processing.analysis.score == 8
