"""Automatic discovery of new sources for the configured interests."""

from .discoverer import (
    ExistingSources,
    SourceDiscoverer,
    SourceRecommendation,
    collect_existing_sources,
)
from .reporter import DiscoveryReporter

__all__ = [
    "ExistingSources",
    "SourceDiscoverer",
    "SourceRecommendation",
    "collect_existing_sources",
    "DiscoveryReporter",
]
