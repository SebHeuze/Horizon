"""Tests for the overridable Jinja2 email templates."""

from datetime import datetime, timezone

from src.ai.summarizer import DailySummarizer
from src.models import (
    ArtifactSource,
    ClassificationResult,
    ContentAnalysis,
    ContentArtifact,
    ContentBlock,
    ContentItem,
    EmailConfig,
    ProcessingResult,
    SourceType,
)
from src.services.email import EmailManager
from src.services.email_render import (
    EmailRenderer,
    build_email_context,
    render_subject,
    score_tier,
)

from tests.test_email import FakeSMTP


def _email_config(**overrides):
    data = {
        "enabled": True,
        "smtp_server": "smtp.example.com",
        "smtp_port": 465,
        "imap_server": "imap.example.com",
        "imap_port": 993,
        "email_address": "noreply@example.com",
        "password_env": "EMAIL_PASSWORD",
    }
    data.update(overrides)
    return EmailConfig(**data)


def _make_item(
    idx: int = 1,
    *,
    title: str = "Stacked PRs land in preview",
    url: str = "https://example.com/items/1",
    score: float = 9.0,
    tags=("github", "workflow"),
) -> ContentItem:
    return ContentItem(
        id=f"rss:item-{idx}",
        source_type=SourceType.RSS,
        title=title,
        url=url,
        content="content",
        author="tester",
        published_at=datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc),
        metadata={"feed_name": "GitHub Changelog"},
        profile="tech-news",
        processing=ProcessingResult(
            classification=ClassificationResult(
                profile="tech-news", method="source_override"
            ),
            analysis=ContentAnalysis(
                score=score, reason="test", summary="Analysis summary.", tags=list(tags)
            ),
            artifacts={
                "en": ContentArtifact(
                    language="en",
                    title=title,
                    blocks=[
                        ContentBlock(
                            id="lead",
                            title="Lead",
                            content="GitHub shipped stacked pull requests.",
                            primary=True,
                        ),
                        ContentBlock(
                            id="background",
                            title="Background",
                            content="Monolithic PRs are hard to review.",
                        ),
                    ],
                    sources=[
                        ArtifactSource(
                            id="s1",
                            title="GitHub Changelog",
                            url="https://github.blog/changelog/stacked-prs/",
                        )
                    ],
                )
            },
        ),
    )


def _send(manager: EmailManager, **kwargs):
    """Send one summary and return (text_body, html_body)."""
    manager.send_daily_summary(
        kwargs.pop("summary", "# Daily"),
        "Subject",
        ["user@example.com"],
        **kwargs,
    )
    message = FakeSMTP.instances[0].messages[0]
    return (
        message.get_payload()[0].get_payload(decode=True).decode(),
        message.get_payload()[1].get_payload(decode=True).decode(),
    )


def _manager(monkeypatch, **config_overrides) -> EmailManager:
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []
    return EmailManager(_email_config(**config_overrides))


def _rich_kwargs(items=None):
    return {
        "items": items if items is not None else [_make_item()],
        "summarizer": DailySummarizer(),
        "date": "2026-07-30",
        "language": "en",
        "total_fetched": 41,
    }


# --- template resolution -------------------------------------------------


def test_user_template_overrides_builtin(monkeypatch, tmp_path):
    (tmp_path / "summary.html.j2").write_text(
        "<p>custom {{ selected_count }}</p>", encoding="utf-8"
    )
    manager = _manager(monkeypatch, template_dir=str(tmp_path))

    _, html_body = _send(manager, **_rich_kwargs())

    assert html_body.strip() == "<p>custom 1</p>"


def test_missing_template_dir_falls_back_to_builtin(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path / "absent"))

    _, html_body = _send(manager, **_rich_kwargs())

    assert "Stacked PRs land in preview" in html_body
    assert "Horizon Daily" in html_body


def test_partial_override_keeps_builtin_text_template(monkeypatch, tmp_path):
    (tmp_path / "summary.html.j2").write_text("<p>custom</p>", encoding="utf-8")
    manager = _manager(monkeypatch, template_dir=str(tmp_path))

    text_body, html_body = _send(manager, summary="# Daily\n\nBody text.")

    assert html_body.strip() == "<p>custom</p>"
    assert "Body text." in text_body
    assert "To unsubscribe" in text_body


def test_broken_user_template_falls_back_without_raising(monkeypatch, tmp_path):
    (tmp_path / "summary.html.j2").write_text("{% for x in %}", encoding="utf-8")
    manager = _manager(monkeypatch, template_dir=str(tmp_path))

    _, html_body = _send(manager, **_rich_kwargs())

    assert "Stacked PRs land in preview" in html_body


def test_template_name_cannot_escape_the_template_dir(monkeypatch, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    templates = tmp_path / "templates"
    templates.mkdir()
    manager = _manager(
        monkeypatch,
        template_dir=str(templates),
        html_template="../secret.txt",
    )

    _, html_body = _send(manager, **_rich_kwargs())

    assert "TOP SECRET" not in html_body
    assert "Horizon Daily" in html_body


# --- rendering -----------------------------------------------------------


def test_rich_rendering_includes_badge_tags_and_references(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path))

    _, html_body = _send(manager, **_rich_kwargs())

    assert "#be185d" in html_body  # score tier "high" badge colour
    assert ">9<" in html_body  # badge shows the bare, rounded score, no "/10"
    assert "#github" in html_body and "#workflow" in html_body
    assert 'href="https://github.blog/changelog/stacked-prs/"' in html_body
    assert "References" in html_body
    assert "「Background」" in html_body
    assert "rss · GitHub Changelog · Jul 30, 08:00" in html_body
    assert "From 41 items, 1 important content pieces were selected" in html_body


def test_degraded_mode_without_items_still_renders_summary(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path))

    _, html_body = _send(manager, summary="# Daily\n\nSomething happened.")

    assert "<h1>Daily</h1>" in html_body
    assert "Something happened." in html_body


def test_theme_overrides_reach_the_template(monkeypatch, tmp_path):
    manager = _manager(
        monkeypatch, template_dir=str(tmp_path), theme={"accent": "#00ff00"}
    )

    _, html_body = _send(manager, **_rich_kwargs())

    assert "#00ff00" in html_body


# --- escaping ------------------------------------------------------------


def test_item_title_is_html_escaped(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path))
    item = _make_item(title="<img src=x onerror=alert(1)>")

    _, html_body = _send(manager, **_rich_kwargs([item]))

    assert "<img src=x" not in html_body
    assert "&lt;img src=x" in html_body


def test_unsafe_item_url_is_dropped(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path))
    item = _make_item()
    # ContentItem.url is an HttpUrl; assignment skips validation by default,
    # which is exactly how a malicious value could reach the renderer.
    item.url = "javascript:alert(1)"

    _, html_body = _send(manager, **_rich_kwargs([item]))

    assert "javascript:alert(1)" not in html_body
    assert "Stacked PRs land in preview" in html_body


def test_unsafe_reference_url_is_dropped(monkeypatch, tmp_path):
    manager = _manager(monkeypatch, template_dir=str(tmp_path))
    item = _make_item()
    item.processing.artifacts["en"].sources[0].url = "javascript:alert(1)"

    _, html_body = _send(manager, **_rich_kwargs([item]))

    assert "javascript:alert(1)" not in html_body
    assert "GitHub Changelog" in html_body


# --- helpers -------------------------------------------------------------


def test_score_tier_thresholds_match_the_web_badges():
    assert score_tier(9.0) == "high"
    assert score_tier(8.9) == "good"
    assert score_tier(7.0) == "good"
    assert score_tier(5.0) == "mid"
    assert score_tier(4.9) == "low"
    assert score_tier("?") == "low"


def test_render_subject_uses_the_configured_template():
    config = _email_config(subject_template="[{lang}] digest {date}")

    assert render_subject(config, lang="EN", date="2026-07-30") == "[EN] digest 2026-07-30"


def test_render_subject_falls_back_on_unknown_placeholder():
    config = _email_config(subject_template="{nope} digest")

    assert (
        render_subject(config, lang="EN", date="2026-07-30")
        == "Horizon Summary (EN) - 2026-07-30"
    )


def test_email_config_defaults_stay_backward_compatible():
    config = _email_config()

    assert config.template_dir == "data/templates/email"
    assert config.html_template == "summary.html.j2"
    assert config.text_template == "summary.txt.j2"
    assert config.theme == {}


def test_context_groups_expose_entries_not_items(tmp_path):
    """`items` would collide with dict.items in Jinja2 attribute lookup."""
    context = build_email_context(
        _email_config(),
        "# Daily",
        "Subject",
        items=[_make_item()],
        summarizer=DailySummarizer(),
        date="2026-07-30",
        total_fetched=41,
    )

    assert [group["profile_id"] for group in context["groups"]] == ["tech-news"]
    assert len(context["groups"][0]["entries"]) == 1
    assert context["groups"][0]["entries"][0]["score_tier"] == "high"


def test_renderer_resolves_relative_template_dir_against_base_dir(tmp_path):
    (tmp_path / "email").mkdir()
    (tmp_path / "email" / "summary.html.j2").write_text("<p>relative</p>", encoding="utf-8")
    renderer = EmailRenderer(_email_config(template_dir="email"), base_dir=tmp_path)

    assert renderer.render_html({"summary_markdown": ""}).strip() == "<p>relative</p>"
