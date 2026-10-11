"""Market context: one consistent read of every public input the engine and monitor need."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from . import economics
from .config import Config
from .feeds import FeedError, PublicFeeds
from .models import FeedFormatError, ProtocolHistory, ProtocolSummary, QueueSummary, TradingState
from .store import Store
from .value_side import PROTOCOL_SAMPLE, ValueSide, compute_value_side, protocol_sample

log = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 300
HISTORY_REFRESH_S = 600
TREND_WINDOW_S = 3 * 3600


@dataclass(frozen=True)
class MarketContext:
    now: float
    summary: ProtocolSummary
    queue: QueueSummary
    trading: TradingState
    relayer_ok: bool
    mids: dict[str, float]
    value: ValueSide
    tail_growth_usd_per_h: float
    tail_growth_source: str
    costs: dict[str, economics.CostEstimate]

    @property
    def spot_mint_rate(self) -> float:
        m = self.summary.mint
        return economics.mint_rate(self.summary.tail_progress_usd, m.flat_rate, m.decay_scale_usd,
                                   self.summary.tracked_lp_usd, m.cliff_usd)

    @property
    def in_flat_region(self) -> bool:
        return self.summary.tracked_lp_usd < self.summary.mint.cliff_usd


class MarketView:
    def __init__(self, cfg: Config, feeds: PublicFeeds, store: Store, baseline: dict, clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.feeds = feeds
        self.store = store
        self.baseline = baseline
        self.clock = clock
        self._history: ProtocolHistory | None = None
        self._history_at = 0.0
        self._last_sample_at = 0.0
        self.last: MarketContext | None = None

    def _history_cached(self, now: float) -> ProtocolHistory | None:
        if self._history is None or now - self._history_at >= HISTORY_REFRESH_S:
            try:
                self._history = self.feeds.history()
                self._history_at = now
            except (FeedError, FeedFormatError) as exc:
                log.warning("protocol history unavailable: %s", exc)
        return self._history

    def refresh(self) -> MarketContext:
        """Raises FeedError/FeedFormatError when a required input is unavailable."""
        now = self.clock()
        summary = self.feeds.summary()
        queue = self.feeds.queue_summary()
        trading = self.feeds.trading_state()
        relayer_ok = self.feeds.relayer_health()
        mids = self.feeds.hl_mids()
        if now - self._last_sample_at >= SAMPLE_INTERVAL_S:
            self.store.add_sample(PROTOCOL_SAMPLE, protocol_sample(summary), ts=now)
            self._last_sample_at = now
        samples = self.store.samples(PROTOCOL_SAMPLE, now - TREND_WINDOW_S)
        value = compute_value_side(summary, queue, self._history_cached(now), samples, self.baseline, now)

        if value.tail_growth_usd_per_h is not None:
            growth, source = max(value.tail_growth_usd_per_h, 0.0), "measured"
        else:
            growth, source = self.cfg.tail_growth_usd_per_h, "configured"

        costs = {}
        for symbol, inst in trading.instruments.items():
            vol = self.cfg.btc_daily_vol if symbol == "BTC" else self.cfg.eth_daily_vol
            costs[symbol] = economics.estimate_cost_per_paper(
                self.cfg.take_profit_move,
                economics.ImpactParams.from_instrument(inst),
                summary.tail_progress_usd,
                growth,
                vol,
                self.cfg.expected_fill_latency_h,
                summary.mint.flat_rate,
                summary.mint.decay_scale_usd,
                summary.tracked_lp_usd,
                summary.mint.cliff_usd,
            )
        self.last = MarketContext(now, summary, queue, trading, relayer_ok, mids, value, growth, source, costs)
        return self.last
