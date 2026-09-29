"""Delivery channels. Each sender gets the alert and the people of its level and returns one result per recipient.

- email: SMTP (Mailpit in the demo stack; a real server via SMTP_* in .env). One mail per person, with the link to
  acknowledge in MES.
- teams: one post to the support channel's incoming webhook (Workflows), naming the people on call.
"""

import asyncio
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from notifier.settings import Settings

log = logging.getLogger(__name__)

SEV_PREFIX = {"P1": "[P1 LINE STOP]", "P2": "[P2]", "P3": "[P3]", "P4": "[P4]"}


def _subject(title: str, alert: dict[str, Any]) -> str:
    prefix = SEV_PREFIX.get(alert.get("severity") or "", "[MES]")
    up = " (escalated)" if alert.get("level") else ""
    return f"{prefix} {title}{up}"[:200]


class Email:
    def __init__(self, s: Settings) -> None:
        self.s = s

    @property
    def configured(self) -> bool:
        return bool(self.s.smtp_host)

    def _send(self, to: str, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = self.s.smtp_from, to, subject
        msg.set_content(text)
        with smtplib.SMTP(self.s.smtp_host, self.s.smtp_port, timeout=15) as smtp:
            if self.s.smtp_starttls:
                smtp.starttls()
            if self.s.smtp_user:
                smtp.login(self.s.smtp_user, self.s.smtp_password)
            smtp.send_message(msg)

    async def __call__(self, channel: str, people: list[dict[str, Any]], title: str, body: str,
                       alert: dict[str, Any]) -> list[tuple[str, bool, str | None]]:
        if not self.configured:
            return [(p["email"], False, "e-mail not configured (SMTP_HOST)") for p in people]
        ack = f"{self.s.mes_ui_url}/notifications?ack={alert['id']}"
        out = []
        for p in people:
            text = (f"Hello {p['name'].split(' (')[0]},\n\n{body}\n\nAcknowledge (so it doesn't escalate): {ack}\n\n"
                    f"-- MES support alerts ({p['role']} on call)")
            try:
                await asyncio.to_thread(self._send, p["email"], _subject(title, alert), text)
                out.append((p["email"], True, None))
            except Exception as e:  # one bad address must not stop the others
                log.warning("e-mail to %s failed: %s", p["email"], e)
                out.append((p["email"], False, f"{type(e).__name__}: {e}"[:300]))
        return out


class Teams:
    def __init__(self, s: Settings, http: httpx.AsyncClient | None = None) -> None:
        self.s = s
        self._http = http or httpx.AsyncClient(timeout=15)

    @property
    def configured(self) -> bool:
        return bool(self.s.teams_webhook_url)

    async def __call__(self, channel: str, people: list[dict[str, Any]], title: str, body: str,
                       alert: dict[str, Any]) -> list[tuple[str, bool, str | None]]:
        people = [p for p in people if p.get("teams", True)]
        if not self.configured or not people:
            return []
        names = ", ".join(p["name"] for p in people)
        card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
            "body": [
                {"type": "TextBlock", "size": "Medium", "weight": "Bolder", "wrap": True, "text": _subject(title, alert),
                 "color": "Attention" if alert.get("severity") in ("P1", "P2") or alert.get("level") else "Default"},
                {"type": "TextBlock", "wrap": True, "text": body},
                {"type": "TextBlock", "wrap": True, "isSubtle": True, "text": f"On call: {names}"},
            ],
            "actions": [{"type": "Action.OpenUrl", "title": "Acknowledge in MES",
                         "url": f"{self.s.mes_ui_url}/notifications?ack={alert['id']}"}],
        }}]}
        try:
            r = await self._http.post(self.s.teams_webhook_url, json=card)
            ok = r.status_code < 300
            return [("teams channel", ok, None if ok else f"HTTP {r.status_code}: {r.text[:200]}")]
        except httpx.HTTPError as e:
            return [("teams channel", False, f"{type(e).__name__}: {e}"[:300])]
