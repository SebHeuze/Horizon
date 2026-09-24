"""Per-stage token accounting."""

import asyncio

import pytest

from src.ai.tokens import (
    UNSTAGED,
    get_usage_snapshot,
    record_usage,
    reset_usage,
    usage_stage,
)


@pytest.fixture(autouse=True)
def _clean_usage():
    reset_usage()
    yield
    reset_usage()


def test_usage_is_attributed_to_the_active_stage():
    record_usage("openai", 5, 1)
    with usage_stage("analysis"):
        record_usage("openai", 10, 2)
        record_usage("decision", 7, 0)
    with usage_stage("enrichment"):
        record_usage("openai", 20, 4)

    snapshot = get_usage_snapshot()
    assert snapshot.per_provider["openai"].input_tokens == 35
    assert list(snapshot.per_stage) == [UNSTAGED, "analysis", "enrichment"]
    assert snapshot.per_stage["analysis"]["openai"].input_tokens == 10
    assert snapshot.per_stage["analysis"]["decision"].input_tokens == 7
    assert snapshot.per_stage["enrichment"]["openai"].output_tokens == 4
    assert snapshot.per_stage[UNSTAGED]["openai"].input_tokens == 5


def test_stage_reaches_tasks_spawned_inside_it():
    async def call():
        await asyncio.sleep(0)
        record_usage("openai", 3, 1)

    async def run():
        with usage_stage("analysis"):
            await asyncio.gather(*(call() for _ in range(4)))
        record_usage("openai", 1, 0)

    asyncio.run(run())
    snapshot = get_usage_snapshot()
    assert snapshot.per_stage["analysis"]["openai"].input_tokens == 12
    assert snapshot.per_stage[UNSTAGED]["openai"].input_tokens == 1
