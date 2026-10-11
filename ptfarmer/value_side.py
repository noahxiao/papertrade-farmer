"""'Value side' analytics: what a staked PAPER is currently earning.

Inputs are the public hourly history (cumulative staker rewards, staked PAPER,
trader PnL), the protocol summary, and our own stored samples of the summary
(tracked LP and tail progress have no public history series).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .guards import next_cap_change, scheduled_staker_cap
from .models import ProtocolHistory, ProtocolSummary, QueueSummary

PROTOCOL_SAMPLE = "protocol"


@dataclass(frozen=True)
class ValueSide:
    staker_rewards_usd_per_day: float | None
    fee_yield_per_paper_day: float | None
    yield_trend_per_day: float | None
    lp_usd: float
    active_cap_usd: float
    lp_fraction_of_cap: float
    next_cap_change: tuple[float, float] | None
    excess_usd: float
    lp_trend_usd_per_h: float | None
    lp_trend_source: str
    tail_growth_usd_per_h: float | None
    queue_open: bool
    queue_usd: float
    queue_count: int


def slope_per_hour(points: list[tuple[float, float]], min_span_s: float = 1200) -> float | None:
    """Least-squares slope (units per hour) of (unix_ts, value) points."""
    if len(points) < 2 or points[-1][0] - points[0][0] < min_span_s:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var == 0:
        return None
    cov = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return cov / var * 3600


def _hourly_deltas(series: list[float]) -> list[float]:
    return [b - a for a, b in zip(series, series[1:])]


YIELD_WINDOW_H = 3


def staker_yield(history: ProtocolHistory, window_h: int = YIELD_WINDOW_H
                 ) -> tuple[float | None, float | None, float | None]:
    """(rewards $/day, $ per staked PAPER per day, yield trend as fractional change per day).

    Uses only completed hourly buckets; the trend compares the latest window
    with the one before it and is expressed as an exponential rate. The window
    is short on purpose: hourly staker revenue has swung by four orders of
    magnitude within a day, and a long mean hides that.
    """
    rewards = history.series("stakingRewards")
    staked = history.series("paperStaked")
    if len(rewards) < window_h + 2 or len(staked) != len(rewards):
        return None, None, None
    deltas = _hourly_deltas(rewards[:-1])
    per_paper = [d / s if s > 0 else 0.0 for d, s in zip(deltas, staked[1:-1])]
    recent = per_paper[-window_h:]
    usd_per_day = sum(deltas[-window_h:]) / window_h * 24
    yield_day = sum(recent) / window_h * 24
    trend = None
    if len(per_paper) >= 2 * window_h:
        prev = sum(per_paper[-2 * window_h:-window_h])
        cur = sum(recent)
        if prev > 0 and cur > 0:
            trend = math.log(cur / prev) / (window_h / 24)
    return usd_per_day, yield_day, trend


def lp_trend_from_history(history: ProtocolHistory, window_h: int = 3) -> float | None:
    """Proxy: LP gains what traders lose. Ignores sweeps to stakers, so it is optimistic."""
    pnl = history.series("traderPnl")
    if len(pnl) < window_h + 1:
        return None
    return -(pnl[-1] - pnl[-1 - window_h]) / window_h


def payback_days(cost_per_paper: float, yield_per_paper_day: float | None, trend_per_day: float | None,
                 horizon_days: float = 365) -> float | None:
    """Days for staking yield to repay `cost_per_paper`; None if not within the horizon.

    With trend k (yield(t) = y0 * e^(k t)), cumulative yield is y0 (e^(kt) - 1) / k.
    """
    if not yield_per_paper_day or yield_per_paper_day <= 0 or cost_per_paper <= 0:
        return None
    k = trend_per_day or 0.0
    if abs(k) < 1e-9:
        days = cost_per_paper / yield_per_paper_day
    else:
        arg = 1 + cost_per_paper * k / yield_per_paper_day
        if arg <= 0:
            return None
        days = math.log(arg) / k
    return days if days <= horizon_days else None


def compute_value_side(summary: ProtocolSummary, queue: QueueSummary, history: ProtocolHistory | None,
                       samples: list[tuple[float, dict]], baseline: dict, now: float) -> ValueSide:
    usd_day = yield_day = trend = None
    if history is not None:
        usd_day, yield_day, trend = staker_yield(history)
    active_cap, _ = scheduled_staker_cap(baseline, now)
    lp_points = [(ts, d["tracked_lp_usd"]) for ts, d in samples if "tracked_lp_usd" in d]
    tail_points = [(ts, d["tail_progress_usd"]) for ts, d in samples if "tail_progress_usd" in d]
    lp_trend, source = slope_per_hour(lp_points), "samples"
    if lp_trend is None and history is not None:
        lp_trend, source = lp_trend_from_history(history), "traderPnl proxy"
    return ValueSide(
        staker_rewards_usd_per_day=usd_day,
        fee_yield_per_paper_day=yield_day,
        yield_trend_per_day=trend,
        lp_usd=summary.tracked_lp_usd,
        active_cap_usd=active_cap,
        lp_fraction_of_cap=summary.tracked_lp_usd / active_cap if active_cap else 0.0,
        next_cap_change=next_cap_change(baseline, now),
        excess_usd=summary.excess_usd,
        lp_trend_usd_per_h=lp_trend,
        lp_trend_source=source if lp_trend is not None else "unavailable",
        tail_growth_usd_per_h=slope_per_hour(tail_points),
        queue_open=queue.is_open,
        queue_usd=queue.total_usd,
        queue_count=queue.count,
    )


def protocol_sample(summary: ProtocolSummary) -> dict:
    return {
        "tracked_lp_usd": summary.tracked_lp_usd,
        "tail_progress_usd": summary.tail_progress_usd,
        "queue_usd": summary.queue_usd,
        "fees_lifetime_usd": summary.fees_lifetime_usd,
        "staked_paper": summary.staked_paper,
        "block": summary.block,
    }
