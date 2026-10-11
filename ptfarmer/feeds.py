"""Read-only clients for Papertrade's public feeds, Hyperliquid mids and HyperEVM RPC."""

from __future__ import annotations

import json
import logging
import time
from typing import Iterator

import requests

from .models import (
    AccountSnapshot,
    ClosedTrade,
    FeedFormatError,
    ProtocolHistory,
    ProtocolSummary,
    QueueSummary,
    TradingState,
)
from .protocol import EXCHANGE_ORIGIN, HYPEREVM_RPC, HYPERLIQUID_INFO

log = logging.getLogger(__name__)

USER_AGENT = "ptfarmer/0.1 (+https://github.com) python-requests"


class FeedError(RuntimeError):
    """A public feed was unreachable or returned an error."""


class PublicFeeds:
    def __init__(
        self,
        origin: str = EXCHANGE_ORIGIN,
        rpc_url: str = HYPEREVM_RPC,
        hl_info_url: str = HYPERLIQUID_INFO,
        timeout_s: float = 15.0,
        session: requests.Session | None = None,
    ):
        self.origin = origin.rstrip("/")
        self.rpc_url = rpc_url
        self.hl_info_url = hl_info_url
        self.timeout_s = timeout_s
        self.http = session or requests.Session()
        self.http.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
        self._price_scales: dict[int, int] = {0: 10, 1: 100}

    def _get_json(self, path: str, params: dict | None = None) -> dict:
        url = self.origin + path
        try:
            resp = self.http.get(url, params=params, timeout=self.timeout_s)
        except requests.RequestException as exc:
            raise FeedError(f"GET {path}: {exc}") from exc
        if resp.status_code != 200:
            raise FeedError(f"GET {path}: HTTP {resp.status_code}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise FeedError(f"GET {path}: invalid JSON") from exc
        if isinstance(data, dict) and data.get("ok") is False:
            raise FeedError(f"GET {path}: {data.get('error') or 'ok=false'}")
        return data

    def summary(self) -> ProtocolSummary:
        return ProtocolSummary.parse(self._get_json("/query/protocol/summary"))

    def queue_summary(self) -> QueueSummary:
        return QueueSummary.parse(self._get_json("/query/protocol/queue-summary"))

    def trading_state(self) -> TradingState:
        state = TradingState.parse(self._get_json("/state/trading"))
        self._price_scales = {inst.id: inst.price_scale for inst in state.instruments.values()}
        return state

    def history(self) -> ProtocolHistory:
        return ProtocolHistory.parse(self._get_json("/query/protocol/history", {"interval": "1h"}))

    def relayer_health(self) -> bool:
        try:
            data = self._get_json("/relayer/health")
        except FeedError:
            return False
        return bool(data.get("ok") and data.get("ready") and data.get("databaseReady") and data.get("pricesReady"))

    def account_trades(self, address: str, limit: int = 75) -> list[ClosedTrade]:
        data = self._get_json(f"/query/accounts/{address.lower()}/trades", {"limit": str(limit)})
        if "items" not in data:
            raise FeedFormatError("trade history missing items")
        return [ClosedTrade.parse(item) for item in data["items"]]

    def account_snapshot(self, address: str, timeout_s: float = 20.0) -> AccountSnapshot:
        """Read the first full `wallet` frame from the account SSE stream, then disconnect."""
        url = f"{self.origin}/state/user/live"
        try:
            with self.http.get(
                url,
                params={"wallet": address.lower()},
                headers={"Accept": "text/event-stream"},
                stream=True,
                timeout=(self.timeout_s, timeout_s),
            ) as resp:
                if resp.status_code != 200:
                    raise FeedError(f"account stream: HTTP {resp.status_code}")
                deadline = time.monotonic() + timeout_s
                for event, data in _iter_sse(resp.iter_lines(decode_unicode=True)):
                    if event == "wallet":
                        payload = json.loads(data)
                        if "baseAccountRevision" in payload:
                            continue
                        return AccountSnapshot.parse(payload, self._price_scales)
                    if time.monotonic() > deadline:
                        break
        except requests.RequestException as exc:
            raise FeedError(f"account stream: {exc}") from exc
        except ValueError as exc:
            raise FeedFormatError(f"account stream: invalid JSON: {exc}") from exc
        raise FeedError("account stream: no wallet snapshot before timeout")

    def hl_mids(self) -> dict[str, float]:
        try:
            resp = self.http.post(self.hl_info_url, json={"type": "allMids"}, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"hyperliquid allMids: {exc}") from exc
        out = {}
        for coin in ("BTC", "ETH"):
            if coin in data:
                out[coin] = float(data[coin])
        if not out:
            raise FeedError("hyperliquid allMids: no BTC/ETH mids")
        return out

    def hl_candles(self, coin: str, interval: str, start_ms: int, end_ms: int) -> list[dict]:
        req = {"coin": coin, "interval": interval, "startTime": start_ms, "endTime": end_ms}
        try:
            resp = self.http.post(self.hl_info_url, json={"type": "candleSnapshot", "req": req}, timeout=self.timeout_s)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"hyperliquid candles {coin}: {exc}") from exc
        if not isinstance(data, list) or any(not isinstance(c, dict) or not {"t", "T", "c"} <= set(c) for c in data):
            raise FeedFormatError(f"hyperliquid candles {coin}: unexpected shape")
        return data

    def rpc(self, method: str, params: list) -> str:
        try:
            resp = self.http.post(
                self.rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=self.timeout_s,
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            raise FeedError(f"rpc {method}: {exc}") from exc
        if "error" in data:
            raise FeedError(f"rpc {method}: {data['error']}")
        return data["result"]

    def frontend_text(self, path: str = "/") -> str:
        try:
            resp = self.http.get(
                self.origin + path,
                headers={"Accept": "text/html,application/javascript", "User-Agent": "Mozilla/5.0"},
                timeout=self.timeout_s,
            )
        except requests.RequestException as exc:
            raise FeedError(f"GET {path}: {exc}") from exc
        if resp.status_code != 200:
            raise FeedError(f"GET {path}: HTTP {resp.status_code}")
        return resp.text


def _iter_sse(lines: Iterator[str]) -> Iterator[tuple[str, str]]:
    event, data = "message", []
    for line in lines:
        if line is None:
            continue
        if line == "":
            if data:
                yield event, "\n".join(data)
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
