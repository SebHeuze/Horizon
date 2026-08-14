"""CLI entry point for automatic source discovery."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import sys
from typing import List, Sequence

from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from .._cli import add_data_dir_arguments, add_log_level_argument
from ..ai.client import create_ai_client
from ..ai.tokens import get_usage_snapshot
from ..console_icons import get_icons
from ..logging_config import configure_logging
from ..models import Config
from ..services.webhook import WebhookNotifier
from ..storage.manager import ConfigError, StorageManager
from .discoverer import SourceDiscoverer, SourceRecommendation, collect_existing_sources
from .reporter import DiscoveryReporter

console = Console(stderr=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="horizon-discover",
        description="Discover new feeds for your interests and report the good ones",
    )
    parser.add_argument(
        "-t", "--topic",
        action="append",
        dest="topics",
        metavar="TOPIC",
        help="Topic to search for; repeatable. Defaults to discovery.topics in config.",
    )
    parser.add_argument(
        "-m", "--max-per-topic",
        type=int,
        metavar="N",
        help="Maximum sources to keep per topic (default: discovery.max_per_topic)",
    )
    parser.add_argument(
        "-o", "--output",
        metavar="PATH",
        help="Report path (default: discovery.output_path)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when discovery.enabled is false",
    )
    parser.add_argument(
        "--no-webhook",
        action="store_true",
        help="Do not send the webhook notification",
    )
    add_data_dir_arguments(parser)
    add_log_level_argument(parser)
    return parser


def resolve_topics(config: Config, requested: Sequence[str] | None) -> List[str]:
    """Pick the topics to search for, most explicit source first.

    CLI flags win over ``discovery.topics``; with neither, the categories
    already used by the configured RSS feeds describe the user's interests
    well enough to search on.
    """
    if requested:
        return [topic.strip() for topic in requested if topic.strip()]
    if config.discovery.topics:
        return list(config.discovery.topics)

    categories: List[str] = []
    for feed in config.sources.rss:
        category = (feed.category or "").strip()
        if category and category not in categories:
            categories.append(category)
    return categories


def main() -> None:
    """Main CLI entry point."""
    configure_logging(console)
    args = _build_parser().parse_args()
    configure_logging(console, level=args.log_level)

    load_dotenv()

    storage = StorageManager(data_dir=args.data_dir, config_path=args.config)
    icons = get_icons()
    try:
        config = storage.load_config()
    except FileNotFoundError:
        console.print(
            f"[bold red]{icons['error']} Configuration file not found:[/bold red] "
            f"[cyan]{storage.config_path}[/cyan]"
        )
        sys.exit(1)
    except ConfigError as exc:
        console.print(f"[bold red]{icons['error']} Error loading configuration: {exc}[/bold red]")
        sys.exit(1)
    except Exception as exc:
        console.print(f"[bold red]{icons['error']} Error loading configuration: {exc}[/bold red]")
        sys.exit(1)

    icons = get_icons(config.display.icon_style)

    if not config.discovery.enabled and not args.force:
        console.print(
            f"{icons['warning']} Discovery is disabled "
            "(set [cyan]discovery.enabled[/cyan] to true, or pass [cyan]--force[/cyan])."
        )
        return

    topics = resolve_topics(config, args.topics)
    if not topics:
        console.print(
            f"[bold red]{icons['error']} No topics to search for.[/bold red] "
            "Set [cyan]discovery.topics[/cyan] in the config or pass [cyan]--topic[/cyan]."
        )
        sys.exit(1)

    try:
        asyncio.run(run_discovery(config, storage, args, topics, icons))
    except KeyboardInterrupt:
        console.print(f"\n[yellow]{icons['warning']} Interrupted by user[/yellow]")
        sys.exit(0)
    except Exception as exc:
        console.print(f"\n[bold red]{icons['error']} Discovery failed: {exc}[/bold red]")
        console.print_exception()
        sys.exit(1)


async def run_discovery(
    config: Config,
    storage: StorageManager,
    args: argparse.Namespace,
    topics: Sequence[str],
    icons: dict,
) -> None:
    """Run discovery end to end: search, report, notify."""
    discovery_config = config.discovery
    if args.max_per_topic:
        discovery_config = discovery_config.model_copy(
            update={"max_per_topic": args.max_per_topic}
        )

    output_path = args.output or discovery_config.output_path
    language = discovery_config.language or (config.ai.languages[0] if config.ai.languages else "en")
    existing = collect_existing_sources(config)

    console.print(f"{icons['start']} Source discovery")
    console.print(f"{icons['detail']} Topics: [cyan]{', '.join(topics)}[/cyan]")
    console.print(
        f"{icons['detail']} Already subscribed (excluded): [cyan]{existing.total}[/cyan]"
    )

    discoverer = SourceDiscoverer(
        discovery_config,
        create_ai_client(config.ai),
        existing,
        console=console,
        icons=icons,
        language=language,
    )
    try:
        recommendations = await discoverer.discover(topics)
    finally:
        await discoverer.aclose()

    reporter = DiscoveryReporter()
    report = reporter.generate_report(
        recommendations,
        topics,
        date=datetime.now().strftime("%Y-%m-%d"),
        skipped_existing=existing.total,
    )
    saved_path = reporter.save_report(report, output_path)
    console.print(f"\n{icons['save']} Report saved to [cyan]{saved_path}[/cyan]")

    _print_recommendations(recommendations, icons)
    _print_token_usage(icons)

    if args.no_webhook:
        return
    if not config.webhook or not config.webhook.enabled:
        return

    notifier = WebhookNotifier(config.webhook, console=console, icons=icons)
    await notifier.send_discovery_report(
        recommendations=recommendations,
        topics=list(topics),
        date=datetime.now().strftime("%Y-%m-%d"),
        report_path=str(saved_path),
        lang=language,
    )


def _print_recommendations(
    recommendations: Sequence[SourceRecommendation], icons: dict
) -> None:
    if not recommendations:
        console.print(
            f"{icons['warning']} No new source cleared the quality threshold."
        )
        return

    table = Table(show_header=True, header_style="bold cyan")
    table.add_column("Score", justify="right")
    table.add_column("Source")
    table.add_column("Topic")
    table.add_column("Feed", overflow="fold")

    for recommendation in recommendations:
        table.add_row(
            f"{recommendation.quality_score:.1f}",
            recommendation.name,
            recommendation.topic,
            recommendation.feed_url,
        )

    console.print(f"\n{icons['filter']} {len(recommendations)} new source(s)")
    console.print(table)


def _print_token_usage(icons: dict) -> None:
    usage = get_usage_snapshot()
    if usage.total_tokens <= 0:
        return
    console.print(
        f"\n{icons['tokens']} Token usage this run: {usage.total_tokens} tokens "
        f"(input: {usage.total_input_tokens}, output: {usage.total_output_tokens})"
    )


if __name__ == "__main__":
    main()
