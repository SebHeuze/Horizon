"""Lightweight token usage tracker shared across AI clients.

This module keeps a simple in-memory counter of tokens used during a single
Horizon run, so the orchestrator can print a summary at the end.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Dict, Iterator, Optional

# Usage recorded outside any usage_stage() block lands under this name.
UNSTAGED = "other"


@dataclass
class ProviderUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class TokenUsageSnapshot:
    total_input_tokens: int
    total_output_tokens: int
    per_provider: Dict[str, ProviderUsage] = field(default_factory=dict)
    # stage -> provider -> usage, in the order stages first recorded tokens.
    per_stage: Dict[str, Dict[str, ProviderUsage]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens


_provider_usage: Dict[str, ProviderUsage] = {}
_stage_usage: Dict[str, Dict[str, ProviderUsage]] = {}
# A ContextVar rather than a global: asyncio tasks spawned inside a stage
# (gather over items) inherit it, and concurrent stages cannot clobber it.
_current_stage: ContextVar[Optional[str]] = ContextVar("horizon_usage_stage", default=None)


@contextmanager
def usage_stage(name: str) -> Iterator[None]:
    """Attribute token usage recorded inside the block to pipeline stage ``name``."""
    token = _current_stage.set(name)
    try:
        yield
    finally:
        _current_stage.reset(token)


def record_usage(provider: str, input_tokens: int = 0, output_tokens: int = 0) -> None:
    """Accumulate token usage for a given provider.

    Args:
        provider: Provider identifier, e.g. "openai", "anthropic".
        input_tokens: Prompt / input tokens used.
        output_tokens: Completion / output tokens used.
    """
    if input_tokens <= 0 and output_tokens <= 0:
        return

    for usage in (
        _provider_usage.setdefault(provider, ProviderUsage()),
        _stage_usage.setdefault(_current_stage.get() or UNSTAGED, {}).setdefault(
            provider, ProviderUsage()
        ),
    ):
        usage.input_tokens += max(0, input_tokens)
        usage.output_tokens += max(0, output_tokens)


def get_usage_snapshot() -> TokenUsageSnapshot:
    """Return a snapshot of accumulated token usage."""
    total_in = sum(u.input_tokens for u in _provider_usage.values())
    total_out = sum(u.output_tokens for u in _provider_usage.values())
    return TokenUsageSnapshot(
        total_input_tokens=total_in,
        total_output_tokens=total_out,
        per_provider=dict(_provider_usage),
        per_stage={stage: dict(usage) for stage, usage in _stage_usage.items()},
    )


def reset_usage() -> None:
    """Reset all accumulated usage (useful for tests)."""
    _provider_usage.clear()
    _stage_usage.clear()
