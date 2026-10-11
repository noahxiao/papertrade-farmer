"""Close distance and leverage for a round (CLOSE_MODE=fixed | vol_matched).

vol_matched picks the close distance from realised BTC volatility so a round
completes in a few hours whatever the regime:

  d = clamp(VOL_MULTIPLIER x sigma_hourly(last VOL_WINDOW_H), CLOSE_MIN, CLOSE_MAX)
  d = CLOSE_MIN while spending is behind the pace schedule
  L = floor(min(1 / (d + VOL_LEVERAGE_BUFFER),          # latency + bust buffer
                1 / (d / TP_MAX_FRACTION_OF_LIQ + bust),  # take-profit <= 60% of liquidation
                MAX_LEVERAGE, market max))

Each repeated slow/overshooting close widens the buffer by one derate step
(lower leverage) before new rounds are paused.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Config

FIXED = "fixed"
VOL_MATCHED = "vol_matched"
INTERVAL_SECONDS = {"5m": 300, "15m": 900, "1h": 3600}
CLOSE_QUALITY_KEY = "close_latency"  # kv: slow/overshoot streaks, derate steps, pause


@dataclass(frozen=True)
class VolReading:
    sigma_hourly: float | None
    source: str  # measured | stale | unavailable | not_used (fixed mode)
    samples: int = 0
    at: float = 0.0


@dataclass(frozen=True)
class CloseShape:
    mode: str
    tp_move: float
    leverage: int
    liq_move: float
    emergency_distance: float
    sigma_hourly: float | None
    sigma_source: str
    behind_pace: bool = False
    derate_steps: int = 0
    note: str = ""

    @property
    def tp_fraction_of_liq(self) -> float:
        return self.tp_move / self.liq_move

    @property
    def gap_to_liq(self) -> float:
        return self.liq_move - self.tp_move


def realised_hourly_vol(closes: list[float], interval_s: float) -> float | None:
    """Zero-mean RMS of log returns, scaled to one hour. None with fewer than 4 returns."""
    rets = [math.log(b / a) for a, b in zip(closes, closes[1:]) if a > 0 and b > 0]
    if len(rets) < 4:
        return None
    return math.sqrt(sum(r * r for r in rets) / len(rets)) * math.sqrt(3600 / interval_s)


def completed_closes(candles: list[dict], now_ms: int, window_h: float, interval_s: float) -> list[float]:
    """Closes of completed candles covering the last `window_h` hours (one extra for the first return)."""
    done = sorted((c for c in candles if int(c["T"]) < now_ms), key=lambda c: int(c["t"]))
    n = int(round(window_h * 3600 / interval_s)) + 1
    return [float(c["c"]) for c in done[-n:]]


def max_safe_leverage(d: float, buffer: float, tp_max_fraction: float, bust_buffer: float, cap: int) -> int:
    by_buffer = 1.0 / (d + buffer)
    by_fraction = 1.0 / (d / tp_max_fraction + bust_buffer)
    return max(1, min(int(math.floor(min(by_buffer, by_fraction) + 1e-9)), cap))


def choose_shape(cfg: Config, market: str, vol: VolReading, bust_buffer: float, market_max_leverage: int,
                 behind_pace: bool = False, derate_steps: int = 0) -> CloseShape:
    cap = min(cfg.max_leverage, market_max_leverage)
    if cfg.close_mode == FIXED:
        lev = min(cfg.leverage, cap)
        liq = 1.0 / lev - bust_buffer
        return CloseShape(FIXED, cfg.take_profit_move, lev, liq, _emergency(cfg, liq, cfg.take_profit_move),
                          vol.sigma_hourly, vol.source, note="fixed close distance and leverage")

    if vol.sigma_hourly is None:
        d, note = cfg.close_max, "volatility unavailable: widest close"
    else:
        raw = cfg.vol_multiplier * vol.sigma_hourly
        d = min(max(raw, cfg.close_min), cfg.close_max)
        note = f"{cfg.vol_multiplier:g} x sigma {vol.sigma_hourly:.3%}/h = {raw:.3%}"
        if d != raw:
            note += f", clamped to {d:.2%}"
    if behind_pace and d > cfg.close_min:
        d, note = cfg.close_min, note + f"; behind pace: tightened to {cfg.close_min:.2%}"
    buffer = cfg.vol_leverage_buffer + derate_steps * cfg.leverage_derate_step
    lev = max_safe_leverage(d, buffer, cfg.tp_max_fraction_of_liq, bust_buffer, cap)
    if derate_steps:
        note += f"; leverage derated {derate_steps} step(s)"
    liq = 1.0 / lev - bust_buffer
    return CloseShape(VOL_MATCHED, d, lev, liq, _emergency(cfg, liq, d), vol.sigma_hourly, vol.source,
                      behind_pace, derate_steps, note)


def _emergency(cfg: Config, liq: float, tp: float) -> float:
    """Early-close distance from bust: never so wide that it fires before the take-profit."""
    return min(cfg.emergency_bust_distance, (liq - tp) / 2)
