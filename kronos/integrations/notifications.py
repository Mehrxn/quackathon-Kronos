"""Dev notifications — Slack webhook and SMTP email for incident lifecycle updates."""

from __future__ import annotations

import os
import asyncio
import enum
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import httpx

from kronos.config import Config
from kronos.models import Incident, Priority

log = logging.getLogger("kronos.notifications")

_DEFAULT_EVENTS = [
    "incident_started",
    "diagnosis_complete",
    "pr_opened",
    "issue_opened",
    "failed",
]


class NotificationEvent(str, enum.Enum):
    INCIDENT_STARTED = "incident_started"
    DIAGNOSIS_COMPLETE = "diagnosis_complete"
    FIXING = "fixing"
    PR_OPENED = "pr_opened"
    ISSUE_OPENED = "issue_opened"
    FAILED = "failed"
    RESOLVED = "resolved"
    IGNORED = "ignored"


_EVENT_TITLES = {
    NotificationEvent.INCIDENT_STARTED: "Incident started",
    NotificationEvent.DIAGNOSIS_COMPLETE: "Diagnosis complete",
    NotificationEvent.FIXING: "Auto-fix in progress",
    NotificationEvent.PR_OPENED: "Pull request opened",
    NotificationEvent.ISSUE_OPENED: "GitHub issue opened",
    NotificationEvent.FAILED: "Incident failed",
    NotificationEvent.RESOLVED: "Incident resolved",
    NotificationEvent.IGNORED: "Incident ignored",
}


class DevNotifier:
    """Notify developers on Slack and/or email when incidents change state."""

    def __init__(self, config: Config):
        self.cfg = config.get("notifications", {}) or {}
        self.enabled = bool(self.cfg.get("enabled", False))
        self.events = set(self.cfg.get("events", _DEFAULT_EVENTS))
        self.min_priority = Priority(
            self.cfg.get("min_priority", "low").lower()
        )
        port = int(os.environ.get("KRONOS_PORT", "8000"))
        base_url = self.cfg.get("dashboard_url", "http://localhost:{port}/dashboard")
        self.dashboard_url = base_url.format(port=port)

        slack = self.cfg.get("slack", {}) or {}
        self.slack_enabled = bool(slack.get("enabled", False))
        self.slack_webhook = slack.get("webhook_url", "")
        self.slack_mention = slack.get("mention", "")  # e.g. <!channel> or @oncall

        email = self.cfg.get("email", {}) or {}
        self.email_enabled = bool(email.get("enabled", False))
        self.smtp_host = email.get("smtp_host", "")
        self.smtp_port = int(email.get("smtp_port", 587))
        self.smtp_user = email.get("smtp_user", "")
        self.smtp_password = email.get("smtp_password", "")
        self.smtp_use_tls = bool(email.get("use_tls", True))
        self.from_address = email.get("from_address", "kronos@localhost")
        self.to_addresses: list[str] = list(email.get("to_addresses", []))

        self._client: Optional[httpx.AsyncClient] = None
        if self.enabled and self.slack_enabled and self.slack_webhook:
            self._client = httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    def configured_channels(self) -> dict[str, bool]:
        return {
            "slack": self.slack_enabled and bool(self.slack_webhook),
            "email": self.email_enabled
            and bool(self.smtp_host)
            and bool(self.to_addresses),
        }

    def _priority_ok(self, incident: Incident) -> bool:
        prio = incident.resolved_priority or incident.declared_priority
        if prio is None and incident.diagnosis:
            prio = incident.diagnosis.priority
        if prio is None:
            return True
        return prio.rank >= self.min_priority.rank

    async def notify(
        self,
        incident: Incident,
        event: NotificationEvent | str,
        *,
        detail: str = "",
    ) -> None:
        if not self.enabled:
            return
        event_key = event.value if isinstance(event, NotificationEvent) else event
        if event_key not in self.events:
            return
        if not self._priority_ok(incident):
            return

        channels = self.configured_channels()
        if not any(channels.values()):
            log.debug("Notifications enabled but no channel configured")
            return

        subject, plain, slack_text = self._format_message(
            incident, event_key, detail
        )

        tasks: list[asyncio.Task] = []
        if channels["slack"]:
            tasks.append(
                asyncio.create_task(
                    self._send_slack(slack_text, incident, event_key)
                )
            )
        if channels["email"]:
            tasks.append(
                asyncio.create_task(
                    asyncio.to_thread(
                        self._send_email, subject, plain, incident.incident_id
                    )
                )
            )

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.warning("Notification delivery failed: %s", r)

    def _format_message(
        self, incident: Incident, event: str, detail: str
    ) -> tuple[str, str, str]:
        title = _EVENT_TITLES.get(
            NotificationEvent(event),
            event.replace("_", " ").title(),
        )
        prio = (
            incident.resolved_priority
            or incident.declared_priority
            or (incident.diagnosis.priority if incident.diagnosis else None)
        )
        prio_str = prio.value.upper() if prio else "UNKNOWN"
        service = incident.service or "unknown service"

        subject = f"[Kronos][{prio_str}] {title} — {incident.incident_id[:12]}"

        lines = [
            f"{title}",
            f"Incident:  {incident.incident_id}",
            f"Service:   {service}",
            f"Priority:  {prio_str}",
            f"Status:    {incident.status.value}",
        ]
        if incident.diagnosis:
            lines.append(f"Confidence: {incident.diagnosis.confidence:.0%}")
            if incident.diagnosis.root_cause:
                lines.append(f"Root cause: {incident.diagnosis.root_cause}")
        if incident.pr_url:
            lines.append(f"PR:        {incident.pr_url}")
        if incident.issue_url:
            lines.append(f"Issue:     {incident.issue_url}")
        if incident.cache_result:
            lines.append(f"Cache:     {incident.cache_result}")
        if detail:
            lines.append(f"Detail:    {detail}")
        if incident.error:
            lines.append(f"Error:     {incident.error}")
        lines.append(f"Dashboard: {self.dashboard_url}")

        plain = "\n".join(lines)

        slack_lines = [f"*{title}* ({prio_str})"]
        if self.slack_mention:
            slack_lines.append(self.slack_mention)
        slack_lines.extend(lines[1:])
        slack_text = "\n".join(slack_lines)

        return subject, plain, slack_text

    async def _send_slack(
        self, text: str, incident: Incident, event: str
    ) -> None:
        if not self._client or not self.slack_webhook:
            return
        payload = {
            "text": text,
            "blocks": [
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text},
                },
            ],
        }
        link = incident.pr_url or incident.issue_url
        if link:
            payload["blocks"].append(
                {
                    "type": "actions",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "View on GitHub"},
                            "url": link,
                        }
                    ],
                }
            )
        resp = await self._client.post(self.slack_webhook, json=payload)
        resp.raise_for_status()
        log.info("Slack notification sent (%s) for %s", event, incident.incident_id)

    def _send_email(
        self, subject: str, body: str, incident_id: str
    ) -> None:
        if not self.smtp_host or not self.to_addresses:
            return
        msg = MIMEMultipart()
        msg["From"] = self.from_address
        msg["To"] = ", ".join(self.to_addresses)
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=30) as smtp:
            if self.smtp_use_tls:
                smtp.starttls()
            if self.smtp_user and self.smtp_password:
                smtp.login(self.smtp_user, self.smtp_password)
            smtp.sendmail(self.from_address, self.to_addresses, msg.as_string())
        log.info("Email notification sent for %s", incident_id)
