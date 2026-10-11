"""HTTP client for Papertrade's (undocumented) relayer intake.

Every submission resolves to exactly one of:
  ACCEPTED     the relayer queued it (it may still fail, expire or land late)
  REJECTED     a definite error code: it was never queued
  UNCONFIRMED  timeout / network error / 5xx / unknown error: it MAY be queued
               and must be treated as live until its deadline passes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import requests

from .protocol import DEFINITE_REJECTION_CODES, EXCHANGE_ORIGIN

log = logging.getLogger(__name__)


class SubmitStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class SubmitResult:
    status: SubmitStatus
    intent_id: str
    code: str | None = None
    reason: str | None = None
    retry_after_s: float | None = None
    http_status: int | None = None

    @property
    def transport_failure(self) -> bool:
        """True when the failure says something about relayer health, not about the intent."""
        return self.status is SubmitStatus.UNCONFIRMED or self.code == "rate_limited"


class RelayerClient:
    def __init__(self, origin: str = EXCHANGE_ORIGIN, build_id: str = "", timeout_s: float = 35.0, session: requests.Session | None = None):
        self.origin = origin.rstrip("/")
        self.timeout_s = timeout_s
        self.http = session or requests.Session()
        self.build_id = build_id

    def _headers(self) -> dict:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": self.origin,
            "Referer": self.origin + "/",
        }
        if self.build_id:
            headers["X-Papertrade-Build"] = self.build_id
        return headers

    def submit(self, action: str, signed: dict, intent_id: str) -> SubmitResult:
        return self._post("/intents", {"action": action, "signed": signed}, intent_id, action)

    def cancel(self, intent_id: str, signed_cancel: dict) -> SubmitResult:
        body = {k: v for k, v in signed_cancel.items() if k != "intentId"}
        return self._post(f"/intents/{intent_id}/cancel", body, intent_id, "cancel")

    def _post(self, path: str, body: dict, intent_id: str, action: str) -> SubmitResult:
        try:
            resp = self.http.post(self.origin + path, json=body, headers=self._headers(), timeout=self.timeout_s, allow_redirects=False)
        except requests.RequestException as exc:
            log.warning("relayer %s %s: transport error %s", action, intent_id, exc)
            return SubmitResult(SubmitStatus.UNCONFIRMED, intent_id, reason=str(exc))
        return classify_response(resp.status_code, _safe_json(resp), resp.headers.get("Retry-After"), intent_id)


def _safe_json(resp: requests.Response) -> dict:
    if resp.status_code == 204:
        return {}
    try:
        data = resp.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def classify_response(status: int, data: dict, retry_after: str | None, intent_id: str) -> SubmitResult:
    code = data.get("code") or data.get("error")
    code = code if isinstance(code, str) else None
    if status == 429 and not code:
        code = "rate_limited"
    reason = data.get("reason") if isinstance(data.get("reason"), str) else None
    retry = None
    if retry_after:
        try:
            retry = float(retry_after)
        except ValueError:
            retry = None

    ok = 200 <= status < 300 and not data.get("error") and data.get("ok") is not False
    if ok:
        server_id = data.get("intentId")
        if isinstance(server_id, str) and server_id.lower() != intent_id.lower():
            log.warning("relayer returned intentId %s, computed %s; using the relayer's", server_id, intent_id)
            return SubmitResult(SubmitStatus.ACCEPTED, server_id.lower(), code="intent_id_mismatch", http_status=status)
        return SubmitResult(SubmitStatus.ACCEPTED, intent_id, http_status=status)
    if code in DEFINITE_REJECTION_CODES:
        return SubmitResult(SubmitStatus.REJECTED, intent_id, code=code, reason=reason, retry_after_s=retry, http_status=status)
    return SubmitResult(SubmitStatus.UNCONFIRMED, intent_id, code=code or "request_failed", reason=reason, retry_after_s=retry, http_status=status)
