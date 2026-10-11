"""Alerts: always logged and stored; optionally POSTed to a webhook.

The webhook body carries both `text` (Slack-style) and `content`
(Discord-style) so a plain incoming-webhook URL works for either.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import requests

log = logging.getLogger("ptfarmer.alerts")


class Notifier:
    def __init__(self, webhook_url: str = "", store=None, clock: Callable[[], float] = time.time,
                 default_cooldown_s: float = 3600, session: requests.Session | None = None, prefix: str = "[ptfarmer]"):
        self.webhook_url = webhook_url
        self.store = store
        self.clock = clock
        self.default_cooldown_s = default_cooldown_s
        self.http = session or requests.Session()
        self.prefix = prefix
        self._last_sent: dict[str, float] = {}
        self.sent: list[tuple[str, str]] = []

    def alert(self, kind: str, message: str, level: str = "warning", dedupe_key: str | None = None,
              cooldown_s: float | None = None) -> bool:
        key = dedupe_key or kind
        now = self.clock()
        cooldown = self.default_cooldown_s if cooldown_s is None else cooldown_s
        last = self._last_sent.get(key)
        if last is not None and now - last < cooldown:
            return False
        self._last_sent[key] = now
        getattr(log, "error" if level in ("error", "critical") else "warning" if level == "warning" else "info")(
            "%s: %s", kind, message
        )
        if self.store is not None:
            self.store.log_event("alert", level=level, alert_kind=kind, message=message)
        self.sent.append((kind, message))
        self.post(f"{self.prefix} {level.upper()} {kind}: {message}")
        return True

    def clear(self, dedupe_key: str) -> None:
        self._last_sent.pop(dedupe_key, None)

    def post(self, text: str) -> bool:
        if not self.webhook_url:
            return False
        try:
            resp = self.http.post(self.webhook_url, json={"text": text, "content": text[:1900]}, timeout=10)
            return resp.status_code < 300
        except requests.RequestException as exc:
            log.warning("webhook delivery failed: %s", exc)
            return False
