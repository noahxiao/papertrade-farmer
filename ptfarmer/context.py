"""Market context: one consistent read of every public input the engine and monitor need."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Callable

from . import economics
from .config import Config
from .feeds import FeedError, PublicFeeds
from .models import FeedFormatError, ProtocolHistory, ProtocolSummary, QueueSummary, TradingState
from .pace import PaceStatus, pace_status, spent_since
from .store import Store
from .value_side import PROTOCOL_SAMPLE, ValueSide, compute_value_side, protocol_sample
from .volatility import (CLOSE_QUALITY_KEY, INTERVAL_SECONDS, VOL_MATCHED, CloseShape, VolReading, choose_shape, completed_closes,
                         realised_hourly_vol)

log = logging.getLogger(__name__)

SAMPLE_INTERVAL_S = 300
HISTORY_REFRESH_S = 600
TREND_WINDOW_S = 3 * 3600
VOL_REFRESH_S = 300
VOL_RETRY_S = 60
VOL_STALE_S = 3600


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
    shapes: dict[str, CloseShape] = field(default_factory=dict)
    pace: PaceStatus | None = None

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
        self._vol: dict[str, VolReading] = {}
        self._vol_attempt: dict[str, float] = {}
        self._readings: dict[str, VolReading] = {}
        self.last: MarketContext | None = None

    def _vol_reading(self, market: str, now: float) -> VolReading:
        """Realised hourly volatility from Hyperliquid candles; stale for up to 1h, then unavailable."""
        last = self._vol.get(market)
        fresh = last is not None and now - last.at < VOL_REFRESH_S
        if not fresh and now - self._vol_attempt.get(market, 0.0) >= VOL_RETRY_S:
            self._vol_attempt[market] = now
            interval_s = INTERVAL_SECONDS[self.cfg.vol_candle_interval]
            start = now - (self.cfg.vol_window_h + 1) * 3600
            try:
                candles = self.feeds.hl_candles(market, self.cfg.vol_candle_interval, int(start * 1000), int(now * 1000))
                closes = completed_closes(candles, int(now * 1000), self.cfg.vol_window_h, interval_s)
                sigma = realised_hourly_vol(closes, interval_s)
                if sigma is not None:
                    last = self._vol[market] = VolReading(sigma, "measured", len(closes) - 1, now)
                    fresh = True
            except (FeedError, FeedFormatError) as exc:
                log.warning("volatility for %s unavailable: %s", market, exc)
        if last is None:
            return VolReading(None, "unavailable")
        if fresh:
            return last
        if now - last.at < VOL_STALE_S:
            return VolReading(last.sigma_hourly, "stale", last.samples, last.at)
        return VolReading(None, "unavailable", 0, last.at)

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

        readings = {}
        for symbol in trading.instruments:
            wanted = self.cfg.close_mode == VOL_MATCHED and symbol in self.cfg.markets
            readings[symbol] = self._vol_reading(symbol, now) if wanted else VolReading(None, "not_used")
        self._readings = readings
        pace, shapes, costs = self._shape_and_cost(now, summary, trading, growth, readings)
        self.last = MarketContext(now, summary, queue, trading, relayer_ok, mids, value, growth, source, costs, shapes, pace)
        return self.last

    def reshape(self, ctx: MarketContext) -> MarketContext:
        """Re-derive pace, close shapes and costs from the same reads after rounds settled this tick."""
        pace, shapes, costs = self._shape_and_cost(ctx.now, ctx.summary, ctx.trading, ctx.tail_growth_usd_per_h,
                                                   self._readings)
        self.last = replace(ctx, pace=pace, shapes=shapes, costs=costs)
        return self.last

    def _shape_and_cost(self, now: float, summary: ProtocolSummary, trading: TradingState, growth: float,
                        readings: dict[str, VolReading]):
        pace = None
        if self.cfg.pace_schedule:
            pace = pace_status(self.cfg.pace_schedule, spent_since(self.store, self.cfg.pace_start), now)
        derate = int(self.store.kv_get(CLOSE_QUALITY_KEY, {}).get("derate_steps", 0))
        costs, shapes = {}, {}
        for symbol, inst in trading.instruments.items():
            reading = readings.get(symbol, VolReading(None, "not_used"))
            shape = shapes[symbol] = choose_shape(self.cfg, symbol, reading, inst.bust_buffer, inst.max_leverage,
                                                  behind_pace=bool(pace and pace.behind), derate_steps=derate)
            if reading.sigma_hourly is not None:
                vol = reading.sigma_hourly * 24 ** 0.5
            else:
                vol = self.cfg.btc_daily_vol if symbol == "BTC" else self.cfg.eth_daily_vol
            costs[symbol] = economics.estimate_cost_per_paper(
                shape.tp_move,
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
        return pace, shapes, costs
