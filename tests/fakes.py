"""Test doubles: a controllable clock, public feeds and a scripted venue/relayer.

`MockVenue` models what the engine can observe of the relayer and the chain:
submissions are queued as operations and land only when a test says so
(`fill`, `land_close`), so every race (late fills, lost cancels, slow closes,
outages) can be scripted deterministically.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ptfarmer import economics
from ptfarmer.feeds import FeedError
from ptfarmer.guards import load_baseline
from ptfarmer.models import (AccountSnapshot, ClosedTrade, CloseKind, Instrument, MintParams, Operation, Position,
                             ProtocolSummary, QueueSummary, TradingState)
from ptfarmer.protocol import INTENT_LIFETIME_S, WAD
from ptfarmer.relayer import SubmitResult, SubmitStatus
from ptfarmer.venue import PreparedIntent

T0 = 1791676800.0  # 2026-10-11T00:00:00Z: the $5M staker cap is active
BTC_MID = 83_000.0


class FakeClock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def make_instrument(symbol: str = "BTC", **overrides) -> Instrument:
    pinned = load_baseline()["instruments"][symbol]
    values = dict(
        id=pinned["id"], symbol=symbol, price_scale=pinned["price_scale"], active=True, paused=False, retired=False,
        close_only=False, rate_multiplier=pinned["rate_multiplier"], position_multiplier=pinned["position_multiplier"],
        max_leverage=pinned["max_leverage"], max_position_usd=50_000_000.0, bust_buffer=pinned["bust_buffer"],
        base_rate=pinned["base_rate"], reference_notional_usd=pinned["reference_notional_usd"],
        long_oi=10_000.0, short_oi=10_000.0, long_cap=70_000.0, short_cap=70_000.0,
    )
    values.update(overrides)
    return Instrument(**values)


def make_trading(paused_actions: frozenset[str] = frozenset(), trading_paused: bool = False, **inst_overrides) -> TradingState:
    return TradingState(
        block=1, trading_paused=trading_paused, min_margin_usd=10.0, min_notional_usd=10_000.0,
        instruments={"BTC": make_instrument("BTC", **inst_overrides), "ETH": make_instrument("ETH")},
        paused_actions=paused_actions, paper_stake_minimum=1000.0,
    )


def make_summary(tracked_lp: float = 3_100_000.0, tail: float = 69_300_000.0, excess: float = 0.0,
                 cap: float = 5_000_000.0, **overrides) -> ProtocolSummary:
    values = dict(
        block=1, at_ms=int(T0 * 1000), tvl_usd=100e6, lp_usd=tracked_lp, queue_usd=0.0, supply_paper=5e9,
        staked_paper=4.9e9, tracked_lp_usd=tracked_lp, tail_progress_usd=tail, staker_cap_usd=cap, excess_usd=excess,
        fees_pending_usd=0.0, fees_lifetime_usd=24e6, mint=MintParams(100.0, 120e6, 2e6),
    )
    values.update(overrides)
    return ProtocolSummary(**values)


@dataclass
class FakeFeeds:
    """Stands in for PublicFeeds as seen by MarketView."""

    summary_obj: ProtocolSummary = field(default_factory=make_summary)
    queue_obj: QueueSummary = field(default_factory=lambda: QueueSummary(0.0, 0))
    trading_obj: TradingState = field(default_factory=make_trading)
    relayer_ok: bool = True
    mids: dict = field(default_factory=lambda: {"BTC": BTC_MID, "ETH": 3_000.0})
    down: bool = False

    def _check(self) -> None:
        if self.down:
            raise FeedError("feeds down (test)")

    def summary(self) -> ProtocolSummary:
        self._check()
        return self.summary_obj

    def queue_summary(self) -> QueueSummary:
        self._check()
        return self.queue_obj

    def trading_state(self) -> TradingState:
        self._check()
        return self.trading_obj

    def relayer_health(self) -> bool:
        self._check()
        return self.relayer_ok

    def hl_mids(self) -> dict:
        self._check()
        return dict(self.mids)

    def history(self):
        return None

    def set_queue(self, total_usd: float, count: int) -> None:
        self.queue_obj = QueueSummary(total_usd, count)

    def move(self, pct: float, market: str = "BTC") -> float:
        self.mids[market] = BTC_MID * (1 + pct / 100) if market == "BTC" else self.mids[market] * (1 + pct / 100)
        return self.mids[market]


class MockVenue:
    """Scripted relayer + account. Nothing lands unless the test makes it land."""

    def __init__(self, clock: FakeClock, mint_rate: float = 40.0, balance_usd: float = 10_000.0):
        self.clock = clock
        self.mint_rate = mint_rate
        self.balance = balance_usd
        self.paper = 0.0
        self.staked = 0.0
        self.positions: dict[int, Position] = {}
        self.ops: dict[str, dict] = {}
        self.trades: list[ClosedTrade] = []
        self.submitted: list[PreparedIntent] = []
        self.cancels: list[str] = []
        self.responses: list[SubmitResult | SubmitStatus | tuple] = []
        self.down = False
        self.account_down = False
        self.lose_cancels = False
        self.unconfirmed_lands = False  # an UNCONFIRMED submission was in fact queued
        self._cancelled: dict[str, dict] = {}
        self._ids = itertools.count(1)
        self._pids = itertools.count(101)

    # -------------------------------------------------------------- scripting
    def script(self, *responses) -> None:
        """Queue responses for the next submissions: SubmitStatus, or (SubmitStatus, code)."""
        self.responses.extend(responses)

    def ops_of(self, action: str) -> list[dict]:
        return [o for o in self.ops.values() if o["action"] == action]

    def submitted_of(self, action: str) -> list[PreparedIntent]:
        return [p for p in self.submitted if p.action == action]

    def open_intent(self, is_long: bool) -> str:
        return next(o["intent_id"] for o in self.ops_of("openPosition") if o["meta"]["is_long"] == is_long)

    def fill(self, intent_id: str, price: float | None = None) -> Position:
        """Land an open intent (it may have been cancelled: that is the late-fill race)."""
        op = self.ops.pop(intent_id, None) or self._cancelled.pop(intent_id)
        meta = op["meta"]
        price = price or BTC_MID
        liq = 1 / meta["leverage"] - 0.000476041580087574
        bust = price * (1 - liq) if meta["is_long"] else price * (1 + liq)
        pid = next(self._pids)
        pos = Position(pid, meta["instrument_id"], meta["is_long"], False, price, meta["margin_wad"], meta["leverage"],
                       bust, int(self.clock() * 1000), intent_id)
        self.positions[pid] = pos
        self.balance -= meta["margin_wad"] / WAD
        return pos

    def land_close(self, intent_id: str, price: float) -> None:
        op = self.ops.pop(intent_id)
        for pid in op["position_ids"]:
            if pid in self.positions:
                self._settle(self.positions.pop(pid), price, CloseKind.CLOSED)

    def liquidate(self, position_id: int) -> None:
        pos = self.positions.pop(position_id)
        self._settle(pos, pos.bust_price, CloseKind.LIQUIDATED)

    def expire(self, intent_id: str) -> None:
        self.ops.pop(intent_id, None)

    def external_position(self) -> Position:
        pid = next(self._pids)
        pos = Position(pid, 0, True, False, BTC_MID, 50 * WAD, 40, BTC_MID * 0.976, int(self.clock() * 1000), None)
        self.positions[pid] = pos
        return pos

    def _settle(self, pos: Position, price: float, kind: CloseKind) -> None:
        margin = pos.margin_usd
        notional = margin * pos.leverage
        move = (price - pos.entry_price) / pos.entry_price * (1 if pos.is_long else -1)
        if kind is CloseKind.LIQUIDATED:
            pnl, fee, basis = -margin, 0.0, margin
        elif move > 0:
            pnl = notional * move
            fee, basis = pnl * economics.WIN_FEE, 0.0
        else:
            pnl, fee = max(notional * move, -margin), 0.0
            basis = -pnl * (1 - economics.LOSS_CARVE)
        minted = basis * self.mint_rate
        self.balance += margin + pnl - fee
        self.paper += minted
        self.trades.append(ClosedTrade(pos.position_id, pos.instrument_id, pos.is_long, kind, pos.entry_price, price,
                                       margin, pos.leverage, pnl, fee, minted, basis, int(self.clock() * 1000)))

    # ------------------------------------------------------------ Venue port
    def account(self) -> AccountSnapshot:
        if self.account_down:
            raise FeedError("account stream down (test)")
        now = self.clock()
        for iid, op in list(self.ops.items()):
            if now > op["deadline"]:
                del self.ops[iid]
        ops = tuple(Operation(o["intent_id"], o["action"], int(o["accepted_at"] * 1000), o["deadline"], tuple(o["position_ids"]))
                    for o in self.ops.values())
        return AccountSnapshot("0x" + "11" * 20, 1, int(now), self.balance, 0.0, self.paper, self.staked, {},
                               tuple(self.positions.values()), ops, paper_staked_wad=int(self.staked * WAD))

    def prepare_open(self, instrument_id: int, is_long: bool, margin_wad: int, leverage: int) -> PreparedIntent:
        return PreparedIntent(self._id(), "openPosition", {}, 0, int(self.clock()) + INTENT_LIFETIME_S,
                              meta={"instrument_id": instrument_id, "is_long": is_long, "margin_wad": margin_wad, "leverage": leverage})

    def prepare_close(self, position_ids: list[int]) -> PreparedIntent:
        return PreparedIntent(self._id(), "closePositions", {}, 0, int(self.clock()) + INTENT_LIFETIME_S, tuple(sorted(position_ids)))

    def submit(self, intent: PreparedIntent) -> SubmitResult:
        self.submitted.append(intent)
        if self.down:
            status, code = SubmitStatus.UNCONFIRMED, "request_failed"
        else:
            status, code = SubmitStatus.ACCEPTED, None
            if self.responses:
                nxt = self.responses.pop(0)
                status, code = nxt if isinstance(nxt, tuple) else (nxt, None)
        queued = status is SubmitStatus.ACCEPTED or (status is SubmitStatus.UNCONFIRMED and self.unconfirmed_lands)
        if queued:
            self.ops[intent.intent_id] = {"intent_id": intent.intent_id, "action": intent.action, "deadline": intent.deadline,
                                          "accepted_at": self.clock(), "position_ids": list(intent.position_ids), "meta": intent.meta}
        return SubmitResult(status, intent.intent_id, code=code)

    def cancel(self, intent_id: str) -> SubmitResult:
        self.cancels.append(intent_id)
        if self.down:
            return SubmitResult(SubmitStatus.UNCONFIRMED, intent_id, code="request_failed")
        op = self.ops.pop(intent_id, None)
        if op is None:
            return SubmitResult(SubmitStatus.REJECTED, intent_id, code="intent_conflict")
        if self.lose_cancels:
            self._cancelled[intent_id] = op  # relayer acknowledged, but the intent still lands later
        return SubmitResult(SubmitStatus.ACCEPTED, intent_id)

    def closed_trades(self) -> list[ClosedTrade]:
        return list(reversed(self.trades))

    def _id(self) -> str:
        return "0x" + format(next(self._ids), "064x")
