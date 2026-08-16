import copy
import io
from email.generator import BytesGenerator
from email.mime.multipart import MIMEMultipart

import pytest

from src.models import EmailConfig
from src.services.email import EmailManager


class FakeSMTP:
    instances = []

    def __init__(self, server, port, **kwargs):
        self.server = server
        self.port = port
        self.kwargs = kwargs
        self.login_calls = []
        self.messages = []
        self.ehlo_calls = 0
        self.starttls_calls = []
        self.closed = False
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def login(self, username, password):
        self.login_calls.append((username, password))

    def send_message(self, message):
        self.messages.append(message)

    def ehlo(self):
        self.ehlo_calls += 1

    def starttls(self, **kwargs):
        self.starttls_calls.append(kwargs)

    def close(self):
        self.closed = True


class FakeIMAP:
    instances = []

    def __init__(self, server, port):
        FakeIMAP.instances.append((server, port))


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


def test_send_daily_summary_uses_smtp_username_when_configured(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config(smtp_username="resend")
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    smtp = FakeSMTP.instances[0]
    assert smtp.login_calls == [("resend", "secret")]
    assert len(smtp.messages) == 1
    assert isinstance(smtp.messages[0], MIMEMultipart)
    assert smtp.messages[0]["From"] == "Horizon Daily <noreply@example.com>"
    assert smtp.messages[0]["To"] == "user@example.com"


def test_send_daily_summary_falls_back_to_email_address_for_smtp_login(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    config = _email_config()
    manager = EmailManager(config)

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert FakeSMTP.instances[0].login_calls == [("noreply@example.com", "secret")]


def test_open_smtp_uses_starttls_outside_implicit_tls_port(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP", FakeSMTP)
    FakeSMTP.instances = []

    server = EmailManager(_email_config(smtp_port=587))._open_smtp()

    assert server is FakeSMTP.instances[0]
    assert server.ehlo_calls == 2
    assert len(server.starttls_calls) == 1
    assert "context" in server.starttls_calls[0]


def test_open_smtp_closes_connection_when_starttls_fails(monkeypatch):
    class FailingStartTLS(FakeSMTP):
        def starttls(self, **kwargs):
            raise RuntimeError("TLS unavailable")

    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP", FailingStartTLS)
    FakeSMTP.instances = []

    with pytest.raises(RuntimeError, match="TLS unavailable"):
        EmailManager(_email_config(smtp_port=587))._open_smtp()

    assert FakeSMTP.instances[0].closed is True


def test_send_daily_summary_escapes_raw_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())

    manager.send_daily_summary(
        "# Hello\n\n<img src=x onerror=alert(1)>", "Daily", ["user@example.com"]
    )

    html_part = FakeSMTP.instances[0].messages[0].get_payload()[1]
    html_body = html_part.get_payload(decode=True).decode()
    assert "<h1>Hello</h1>" in html_body
    assert "<img src=x" not in html_body
    assert "&lt;img src=x" in html_body


def test_send_daily_summary_cleans_app_generated_markdown_html(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<a id="item-1"></a>
## Item

<details><summary>参考链接</summary>
<ul>
<li><a href="https://example.com/a">Example A</a></li>
<li><a href="https://example.com/b">Example B</a></li>
</ul>
</details>
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    assert '<a id="item-1"></a>' not in text_body
    assert "<details>" not in text_body
    assert "<summary>" not in text_body
    assert "**参考链接**" in text_body
    assert "- [Example A](https://example.com/a)" in text_body

    assert '&lt;a id="item-1"&gt;&lt;/a&gt;' not in html_body
    assert "&lt;details&gt;" not in html_body
    assert "&lt;summary&gt;" not in html_body
    assert "<strong>参考链接</strong>" in html_body
    assert '<a href="https://example.com/a">Example A</a>' in html_body
    assert '<a href="https://example.com/b">Example B</a>' in html_body


def test_send_daily_summary_does_not_link_unsafe_details_href(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    summary = """# Daily

<details><summary>References</summary>
<ul>
<li><a href="javascript:alert(1)">click [me](https://evil.example)</a></li>
</ul>
</details>
"""

    manager.send_daily_summary(summary, "Daily", ["user@example.com"])

    message = FakeSMTP.instances[0].messages[0]
    text_body = message.get_payload()[0].get_payload(decode=True).decode()
    html_body = message.get_payload()[1].get_payload(decode=True).decode()

    assert 'href="javascript:alert(1)"' not in html_body
    assert "[click](javascript:alert(1))" not in text_body
    assert "- click \\[me\\]\\(https://evil.example\\)" in text_body
    assert "click [me](https://evil.example)" in html_body


def test_check_subscriptions_skips_imap_when_disabled(monkeypatch):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.imaplib.IMAP4_SSL", FakeIMAP)
    FakeIMAP.instances = []

    config = _email_config(imap_enabled=False)
    manager = EmailManager(config)

    manager.check_subscriptions(storage_manager=object())

    assert FakeIMAP.instances == []


def _smtplib_bytes(message):
    """Flatten a message the way smtplib.SMTP.send_message does."""
    msg_copy = copy.copy(message)
    del msg_copy["Bcc"]
    del msg_copy["Resent-Bcc"]
    with io.BytesIO() as buffer:
        BytesGenerator(buffer).flatten(msg_copy, linesep="\r\n")
        return buffer.getvalue()


def test_send_daily_summary_dumps_message_sent_to_smtp(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    dump_dir = tmp_path / "sent"
    manager = EmailManager(_email_config(dump_dir=str(dump_dir)))

    manager.send_daily_summary(
        "# Hello", "Daily", ["user@example.com"], date="2026-08-16", language="fr"
    )

    dumped = dump_dir / "horizon-2026-08-16-fr-user_example.com.eml"
    assert dumped.exists()
    assert dumped.read_bytes() == _smtplib_bytes(FakeSMTP.instances[0].messages[0])


def test_send_daily_summary_skips_dump_without_dump_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    manager = EmailManager(_email_config())
    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert list(tmp_path.iterdir()) == []
    assert len(FakeSMTP.instances[0].messages) == 1


def test_dump_message_keeps_hostile_subscriber_inside_dump_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    dump_dir = tmp_path / "sent"
    manager = EmailManager(_email_config(dump_dir=str(dump_dir)))

    message = MIMEMultipart("alternative")
    message["To"] = "../../etc/passwd@example.com"

    path = manager._dump_message(message, "../../etc/passwd@example.com", date="2026-08-16")

    assert path is not None
    assert path.parent == dump_dir.resolve()


def test_dump_failure_does_not_block_send(monkeypatch, tmp_path):
    monkeypatch.setenv("EMAIL_PASSWORD", "secret")
    monkeypatch.setattr("src.services.email.smtplib.SMTP_SSL", FakeSMTP)
    FakeSMTP.instances = []

    blocker = tmp_path / "sent"
    blocker.write_text("not a directory")
    manager = EmailManager(_email_config(dump_dir=str(blocker)))

    manager.send_daily_summary("# Hello", "Daily", ["user@example.com"])

    assert len(FakeSMTP.instances[0].messages) == 1
