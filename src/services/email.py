"""Email service for handling subscriptions and sending summaries."""

import email
import imaplib
import logging
import os
import re
import smtplib
import ssl
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parseaddr
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from rich.console import Console

from ..models import ContentItem, EmailConfig
from ..storage.manager import safe_output_path
from .email_render import EmailRenderer, build_email_context

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..ai.summarizer import DailySummarizer

logger = logging.getLogger(__name__)


class EmailManager:
    """Manages email subscriptions and sending summaries."""

    def __init__(self, config: EmailConfig, console=None):
        self.config = config
        self.pwd = os.getenv(self.config.password_env)
        self.console = console if console is not None else Console(stderr=True)
        self.renderer = EmailRenderer(config)

        if not self.pwd and self.config.enabled:
            logger.warning(
                f"Environment variable {self.config.password_env} not set. Email features may fail."
            )
            self.console.print(
                f"[yellow]Warning: Environment variable {self.config.password_env} not set. Email features may fail.[/yellow]"
            )

    def check_subscriptions(self, storage_manager):
        """Checks inbox for subscription requests and updates subscriber list."""
        if not self.config.enabled or not self.config.imap_enabled:
            return

        try:
            mail = imaplib.IMAP4_SSL(self.config.imap_server, self.config.imap_port)
            mail.login(self.config.email_address, self.pwd)
            mail.select("INBOX")

            keyword = self.config.subscribe_keyword
            search_crit = f'(UNSEEN SUBJECT "{keyword}")'

            status, messages = mail.search(None, search_crit)

            if status == "OK" and messages[0]:
                email_ids = messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr not in subscribers:
                                        storage_manager.add_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Subscribed to Horizon",
                                            "You have been successfully subscribed to Horizon daily summaries.",
                                        )
                                        logger.info(f"Added subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Already subscribed: {email_addr}")

            unsub_keyword = self.config.unsubscribe_keyword
            search_crit_unsub = f'(UNSEEN SUBJECT "{unsub_keyword}")'

            status, messages = mail.search(None, search_crit_unsub)

            if status == "OK" and messages[0]:
                email_ids = messages[0].split()
                subscribers = storage_manager.load_subscribers()

                for e_id in email_ids:
                    _, msg_data = mail.fetch(e_id, "(RFC822)")
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])

                            subject = str(msg.get("Subject") or "").strip()
                            if subject.upper() != unsub_keyword.upper():
                                continue

                            sender = msg.get("From")

                            if sender:
                                _, email_addr = parseaddr(sender)
                                if email_addr and "@" in email_addr:
                                    if (
                                        "noreply" in email_addr.lower()
                                        or "no-reply" in email_addr.lower()
                                    ):
                                        continue

                                    if email_addr in subscribers:
                                        storage_manager.remove_subscriber(email_addr)
                                        subscribers = storage_manager.load_subscribers()
                                        self._send_reply(
                                            email_addr,
                                            "Unsubscribed from Horizon",
                                            "You have been successfully unsubscribed from Horizon daily summaries.",
                                        )
                                        logger.info(f"Removed subscriber: {email_addr}")
                                    else:
                                        logger.info(f"Not subscribed: {email_addr}")

            mail.close()
            mail.logout()

        except Exception as e:
            logger.error(f"Error checking subscriptions: {e}")

    def _open_smtp(self) -> smtplib.SMTP:
        """Open an implicit TLS or STARTTLS SMTP connection."""
        context = ssl.create_default_context()
        if self.config.smtp_port == 465:
            return smtplib.SMTP_SSL(
                self.config.smtp_server, self.config.smtp_port, context=context
            )

        server = smtplib.SMTP(self.config.smtp_server, self.config.smtp_port)
        try:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
        except Exception:
            server.close()
            raise
        return server

    def _dump_message(
        self,
        msg: MIMEMultipart,
        subscriber: str,
        *,
        date: str = "",
        language: str = "en",
    ) -> Optional[Path]:
        """Write the message to ``dump_dir`` as a ``.eml`` file.

        The bytes are flattened the way :meth:`smtplib.SMTP.send_message`
        flattens them, CRLF line endings included, so the file can be replayed
        verbatim with ``sendmail()``. Returns ``None`` when no ``dump_dir`` is
        configured; a dump failure is logged and never blocks the send.
        """
        if not self.config.dump_dir:
            return None

        try:
            dump_dir = Path(self.config.dump_dir)
            dump_dir.mkdir(parents=True, exist_ok=True)

            safe_subscriber = re.sub(r"[^A-Za-z0-9._-]", "_", subscriber)
            stamp = date or datetime.now().strftime("%Y-%m-%d")
            path = safe_output_path(
                dump_dir, f"horizon-{stamp}-{language}-{safe_subscriber}.eml"
            )

            raw = msg.as_bytes(policy=msg.policy.clone(linesep="\r\n"))
            path.write_bytes(raw)

            logger.info(f"Dumped outgoing email to {path} ({len(raw)} bytes)")
            return path
        except Exception as e:
            logger.error(f"Failed to dump outgoing email for {subscriber}: {e}")
            return None

    def send_daily_summary(
        self,
        summary_md: str,
        subject: str,
        subscribers: List[str],
        *,
        items: Optional[List[ContentItem]] = None,
        summarizer: Optional["DailySummarizer"] = None,
        date: str = "",
        language: str = "en",
        total_fetched: Optional[int] = None,
    ):
        """Sends the daily summary to all subscribers.

        `items` and `summarizer` unlock the rich per-item rendering; without
        them the templates fall back to the Markdown-derived body.
        """
        if not self.config.enabled or not subscribers:
            return

        context = build_email_context(
            self.config,
            summary_md,
            subject,
            items=items,
            summarizer=summarizer,
            date=date,
            language=language,
            total_fetched=total_fetched,
        )
        text_body = self.renderer.render_text(context)
        html_body = self.renderer.render_html(context)

        try:
            with self._open_smtp() as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                for subscriber in subscribers:
                    msg = MIMEMultipart("alternative")
                    msg["Subject"] = subject
                    msg["From"] = (
                        f"{self.config.sender_name} <{self.config.email_address}>"
                    )
                    msg["To"] = subscriber

                    text_part = MIMEText(text_body, "plain")
                    html_part = MIMEText(html_body, "html")

                    msg.attach(text_part)
                    msg.attach(html_part)

                    # Dump the raw message handed to SMTP.
                    self._dump_message(msg, subscriber, date=date, language=language)

                    try:
                        server.send_message(msg)
                        logger.info(f"Sent summary to {subscriber}")
                    except Exception as e:
                        logger.error(f"Failed to send to {subscriber}: {e}")

        except Exception as e:
            logger.error(f"SMTP Error: {e}")

    def _send_reply(self, to_email: str, subject: str, body: str):
        """Helper to send a simple reply."""
        try:
            with self._open_smtp() as server:
                server.login(
                    self.config.smtp_username or self.config.email_address, self.pwd
                )

                msg = MIMEText(body)
                msg["Subject"] = subject
                msg["From"] = f"{self.config.sender_name} <{self.config.email_address}>"
                msg["To"] = to_email

                server.send_message(msg)
        except Exception as e:
            logger.error(f"Failed to send reply to {to_email}: {e}")
