"""Safety guards: protocol rule-change detection and relayer circuit breaking."""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from eth_utils import keccak

from .feeds import FeedError, PublicFeeds
from .models import ProtocolSummary, TradingState
from .protocol import EIP1967_ADMIN_SLOT, EIP1967_IMPLEMENTATION_SLOT, TYPES

log = logging.getLogger(__name__)

BASELINE_PATH = Path(__file__).with_name("baseline.json")


@dataclass(frozen=True)
class Finding:
    severity: str  # "halt" | "warn"
    check: str
    message: str


def load_baseline(path: Path = BASELINE_PATH) -> dict:
    return json.loads(path.read_text())


def _parse_utc(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()


def scheduled_staker_cap(baseline: dict, now: float) -> tuple[float, float | None]:
    """(active cap, previous cap if we are inside the switch-over grace window)."""
    schedule = sorted(baseline["staker_cap_schedule"], key=lambda s: s["from"])
    grace = baseline.get("staker_cap_switch_grace_h", 0) * 3600
    active, previous = schedule[0]["cap_usd"], None
    for entry in schedule:
        start = _parse_utc(entry["from"])
        if now >= start:
            previous = active if now < start + grace and entry is not schedule[0] else None
            active = entry["cap_usd"]
    return active, previous


def next_cap_change(baseline: dict, now: float) -> tuple[float, float] | None:
    for entry in sorted(baseline["staker_cap_schedule"], key=lambda s: s["from"]):
        start = _parse_utc(entry["from"])
        if start > now:
            return start, entry["cap_usd"]
    return None


def _close(a: float, b: float, rel: float = 1e-9) -> bool:
    return abs(a - b) <= rel * max(abs(a), abs(b), 1e-18)


def check_protocol_params(baseline: dict, summary: ProtocolSummary, trading: TradingState, markets: tuple[str, ...], now: float) -> list[Finding]:
    findings: list[Finding] = []
    mint = baseline["mint"]
    for name, live in (("flat_rate", summary.mint.flat_rate), ("decay_scale_usd", summary.mint.decay_scale_usd), ("cliff_usd", summary.mint.cliff_usd)):
        if not _close(live, mint[name]):
            findings.append(Finding("halt", "mint_curve", f"mint {name} changed: pinned {mint[name]}, live {live}"))

    active, previous = scheduled_staker_cap(baseline, now)
    if not _close(summary.staker_cap_usd, active):
        if previous is not None and _close(summary.staker_cap_usd, previous):
            findings.append(Finding("warn", "staker_cap", f"staker cap still {previous:,.0f}; scheduled {active:,.0f}"))
        else:
            findings.append(Finding("halt", "staker_cap", f"staker cap {summary.staker_cap_usd:,.0f} does not match schedule ({active:,.0f})"))

    mins = baseline["minimums"]
    if not _close(trading.min_margin_usd, mins["margin_usd"]) or not _close(trading.min_notional_usd, mins["notional_usd"]):
        findings.append(Finding("halt", "minimums", f"minimums changed: margin {trading.min_margin_usd}, notional {trading.min_notional_usd}"))

    tol = baseline.get("bust_buffer_rel_tolerance", 0.05)
    for symbol in markets:
        pinned = baseline["instruments"].get(symbol)
        inst = trading.instruments.get(symbol)
        if pinned is None or inst is None:
            findings.append(Finding("halt", "instrument", f"{symbol} missing from baseline or /state/trading"))
            continue
        if inst.id != pinned["id"] or inst.price_scale != pinned["price_scale"]:
            findings.append(Finding("halt", "instrument", f"{symbol} id/price scale changed"))
        for name in ("base_rate", "rate_multiplier", "position_multiplier", "reference_notional_usd"):
            if not _close(getattr(inst, name), pinned[name], 1e-9):
                findings.append(Finding("halt", "fee_params", f"{symbol} {name} changed: pinned {pinned[name]}, live {getattr(inst, name)}"))
        if abs(inst.bust_buffer - pinned["bust_buffer"]) > tol * pinned["bust_buffer"]:
            findings.append(Finding("halt", "fee_params", f"{symbol} bust buffer changed: pinned {pinned['bust_buffer']}, live {inst.bust_buffer}"))
        if inst.max_leverage < pinned["max_leverage"]:
            findings.append(Finding("warn", "instrument", f"{symbol} max leverage lowered to {inst.max_leverage}"))
    return findings


def check_contracts(baseline: dict, feeds: PublicFeeds) -> list[Finding]:
    findings: list[Finding] = []
    try:
        for name, address in baseline["contracts"].items():
            code = feeds.rpc("eth_getCode", [address, "latest"])
            if "0x" + keccak(hexstr=code).hex() != baseline["proxy_code_hash"]:
                findings.append(Finding("halt", "bytecode", f"{name} proxy bytecode changed"))
            admin = "0x" + feeds.rpc("eth_getStorageAt", [address, EIP1967_ADMIN_SLOT, "latest"])[-40:]
            if admin.lower() != baseline["proxy_admin"]:
                findings.append(Finding("halt", "proxy_admin", f"{name} proxy admin changed to {admin}"))
            impl = "0x" + feeds.rpc("eth_getStorageAt", [address, EIP1967_IMPLEMENTATION_SLOT, "latest"])[-40:]
            pinned = baseline["implementations"][name]
            if impl.lower() != pinned["address"]:
                findings.append(Finding("halt", "upgrade", f"{name} implementation upgraded to {impl}"))
                continue
            impl_code = feeds.rpc("eth_getCode", [impl, "latest"])
            if "0x" + keccak(hexstr=impl_code).hex() != pinned["code_hash"]:
                findings.append(Finding("halt", "bytecode", f"{name} implementation bytecode changed"))
    except FeedError as exc:
        findings.append(Finding("warn", "rpc", f"could not verify contracts: {exc}"))
    return findings


_ASSET_RE = re.compile(r"/assets/((?:typed-data|deployment|build)-[A-Za-z0-9_-]+\.js)")


def _typed_fields(chunk: str, type_name: str) -> list[tuple[str, str]] | None:
    m = re.search(type_name + r":\[(.*?)\]\}?(?:,[A-Z]|\})", chunk)
    if not m:
        return None
    return re.findall(r"name:`([^`]+)`,type:`([^`]+)`", m.group(1))


def check_frontend(baseline: dict, feeds: PublicFeeds) -> tuple[list[Finding], str | None]:
    """Compare the live frontend's typed data and deployment config with ours.

    Returns the findings and the frontend build id (used for X-Papertrade-Build).
    """
    findings: list[Finding] = []
    try:
        html = feeds.frontend_text("/")
        assets = dict((name.split("-")[0], name) for name in _ASSET_RE.findall(html))
        if "typed" not in assets or "deployment" not in assets:
            return [Finding("warn", "frontend", "could not locate typed-data/deployment chunks")], None
        typed = feeds.frontend_text("/assets/" + assets["typed"])
        deployment = feeds.frontend_text("/assets/" + assets["deployment"])
        build_id = None
        if "build" in assets:
            m = re.search(r"`([0-9a-f]{8,})`", feeds.frontend_text("/assets/" + assets["build"]))
            build_id = m.group(1) if m else None
    except FeedError as exc:
        return [Finding("warn", "frontend", f"could not fetch frontend: {exc}")], None

    for type_name in ("OpenPosition", "Close", "CancelIntent", "Stake"):
        live = _typed_fields(typed, type_name)
        ours = [(f["name"], f["type"]) for f in TYPES[type_name]]
        if live is None:
            findings.append(Finding("warn", "typed_data", f"{type_name} not found in frontend typed data"))
        elif live != ours:
            findings.append(Finding("halt", "typed_data", f"{type_name} typed data changed: {live}"))
    fe = baseline["frontend"]
    if f"`{fe['intent_id_tag']}`" not in typed:
        findings.append(Finding("halt", "typed_data", "intent-id tag changed"))
    if f"name:`{fe['exchange_domain']}`" not in typed or fe["cancel_domain"] not in typed:
        findings.append(Finding("halt", "typed_data", "EIP-712 domain names changed"))
    for name, address in baseline["contracts"].items():
        if address not in deployment.lower():
            findings.append(Finding("halt", "contracts", f"frontend no longer references {name} {address}"))
    if "chainId:999" not in deployment:
        findings.append(Finding("halt", "contracts", "frontend chain id changed"))
    return findings, build_id


class RelayerBreaker:
    """Tracks relayer health and spaces out submissions after failures."""

    def __init__(self, max_consecutive_failures: int, down_halt_s: float, clock: Callable[[], float] = time.time,
                 base_backoff_s: float = 5.0, max_backoff_s: float = 300.0):
        self.max_failures = max_consecutive_failures
        self.down_halt_s = down_halt_s
        self.clock = clock
        self.base = base_backoff_s
        self.cap = max_backoff_s
        self.failures = 0
        self.health_ok = True
        self.next_attempt_at = 0.0
        self.down_since: float | None = None

    def _update_down(self) -> None:
        if self.is_degraded:
            self.down_since = self.down_since or self.clock()
        else:
            self.down_since = None

    @property
    def is_degraded(self) -> bool:
        return (not self.health_ok) or self.failures >= self.max_failures

    @property
    def down_for_s(self) -> float:
        return self.clock() - self.down_since if self.down_since else 0.0

    @property
    def should_halt(self) -> bool:
        return self.down_since is not None and self.down_for_s >= self.down_halt_s

    def can_submit(self) -> bool:
        return self.clock() >= self.next_attempt_at

    def record_health(self, ok: bool) -> None:
        self.health_ok = ok
        self._update_down()

    def record_success(self) -> None:
        self.failures = 0
        self.next_attempt_at = 0.0
        self._update_down()

    def record_failure(self, retry_after_s: float | None = None) -> None:
        self.failures += 1
        backoff = min(self.cap, self.base * 2 ** (self.failures - 1))
        if retry_after_s:
            backoff = max(backoff, retry_after_s)
        self.next_attempt_at = self.clock() + backoff
        self._update_down()
