"""Hedged-cycle economics (pure functions, no I/O).

Formulas follow https://docs.papertrade.xyz/#/how/asymmetric-impact and
https://docs.papertrade.xyz/#/how/mint-curve, and were checked against live
closed trades (a winning ETH close reproduced the fee to the cent; a losing BTC
close minted on loss x 0.98 at the ratchet rate).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

DEADBAND = 1.0 / 50_000  # 0.2 bps: exit is treated as this much less favourable
WIN_FEE = 0.02
LOSS_CARVE = 0.02  # LP-side carve from the mint basis while the payout queue is empty


@dataclass(frozen=True)
class ImpactParams:
    base_rate: float
    rate_multiplier: float
    position_multiplier: float
    reference_notional_usd: float

    @classmethod
    def from_instrument(cls, inst) -> "ImpactParams":
        return cls(inst.base_rate, inst.rate_multiplier, inst.position_multiplier, inst.reference_notional_usd)


BTC_LAUNCH_PARAMS = ImpactParams(0.10, 15_000, 814.598, 100_000)
ETH_LAUNCH_PARAMS = ImpactParams(0.10, 15_000, 483.979, 100_000)


def mint_rate(tail_progress_usd: float, flat_rate: float = 100.0, decay_scale_usd: float = 120e6,
              tracked_lp_usd: float | None = None, cliff_usd: float | None = None) -> float:
    """PAPER per $1 of mint basis.

    Flat `flat_rate` while tracked LP is below the cliff (including negative),
    otherwise the ratchet value flat * (S / (S + H))^2. The engine cross-checks
    this against PAPER actually minted on its own closes and halts on a mismatch.
    """
    if tracked_lp_usd is not None and cliff_usd is not None and tracked_lp_usd < cliff_usd:
        return flat_rate
    return flat_rate * (decay_scale_usd / (decay_scale_usd + max(tail_progress_usd, 0.0))) ** 2


def win_scale(move: float, params: ImpactParams) -> float:
    """Impact scale applied to the raw gain, where move already includes the deadband."""
    if move <= 0:
        return 0.0
    term1 = 1.0 / (move * params.rate_multiplier)
    term2 = params.reference_notional_usd / (1e6 * move * params.position_multiplier)
    return (1.0 - params.base_rate) / (1.0 + term1 + term2)


def winning_close_received(move: float, params: ImpactParams) -> float:
    """Net gain credited per $1 of notional for a winning close at `move`."""
    effective = move - DEADBAND
    if effective <= 0:
        return 0.0
    return effective * win_scale(effective, params) * (1.0 - WIN_FEE)


@dataclass(frozen=True)
class CycleEconomics:
    move: float
    loss_per_notional: float
    win_per_notional: float
    net_cost_per_notional: float
    paper_per_notional: float
    mint_rate: float

    @property
    def cost_per_paper(self) -> float:
        return self.net_cost_per_notional / self.paper_per_notional if self.paper_per_notional > 0 else math.inf


def hedged_cycle(move: float, params: ImpactParams, rate: float, queue_empty: bool = True) -> CycleEconomics:
    """Close both legs at `move`: one loses `move`, the other wins `move`."""
    loss = move
    win = winning_close_received(move, params)
    basis = loss * (1.0 - LOSS_CARVE) if queue_empty else loss
    return CycleEconomics(
        move=move,
        loss_per_notional=loss,
        win_per_notional=win,
        net_cost_per_notional=loss - win,
        paper_per_notional=basis * rate,
        mint_rate=rate,
    )


def expected_cycle_hours(move: float, daily_vol: float, fill_latency_h: float = 0.0) -> float:
    """Expected time for |price move| to first reach `move` (driftless Brownian motion).

    E[T] = move^2 / sigma^2 for a symmetric two-sided barrier, plus the time for
    the opens and the close to clear the relayer queue.
    """
    hourly_var = daily_vol**2 / 24.0
    return move**2 / hourly_var + fill_latency_h


def forecast_tail(tail_progress_usd: float, growth_usd_per_h: float, hours: float) -> float:
    return tail_progress_usd + max(growth_usd_per_h, 0.0) * max(hours, 0.0)


@dataclass(frozen=True)
class CostEstimate:
    spot: CycleEconomics
    at_close: CycleEconomics
    cycle_hours: float
    rate_decay_per_hour: float

    @property
    def cost_per_paper(self) -> float:
        return self.at_close.cost_per_paper


def estimate_cost_per_paper(
    move: float,
    params: ImpactParams,
    tail_progress_usd: float,
    tail_growth_usd_per_h: float,
    daily_vol: float,
    fill_latency_h: float,
    flat_rate: float = 100.0,
    decay_scale_usd: float = 120e6,
    tracked_lp_usd: float | None = None,
    cliff_usd: float | None = None,
) -> CostEstimate:
    """Cost per PAPER at the mint rate expected when the losing leg is closed.

    In the flat region the high-water mark does not advance, so the rate is
    assumed to stay flat for the cycle (the LP crossing back above the cliff
    mid-cycle is not modelled).
    """
    hours = expected_cycle_hours(move, daily_vol, fill_latency_h)

    def rate_after(h: float) -> float:
        return mint_rate(forecast_tail(tail_progress_usd, tail_growth_usd_per_h, h), flat_rate, decay_scale_usd,
                         tracked_lp_usd, cliff_usd)

    spot_rate, close_rate, next_hour_rate = rate_after(0.0), rate_after(hours), rate_after(1.0)
    return CostEstimate(
        spot=hedged_cycle(move, params, spot_rate),
        at_close=hedged_cycle(move, params, close_rate),
        cycle_hours=hours,
        rate_decay_per_hour=1.0 - next_hour_rate / spot_rate if spot_rate > 0 else 0.0,
    )


def expected_round_cost_usd(notional_usd: float, move: float, params: ImpactParams) -> float:
    return notional_usd * hedged_cycle(move, params, rate=1.0).net_cost_per_notional


def liquidated_round_cost_usd(margin_usd: float, leverage: int, bust_buffer: float, params: ImpactParams) -> float:
    """Net loss when the losing leg busts and the winner is closed at the same move."""
    notional = margin_usd * leverage
    bust_move = 1.0 / leverage - bust_buffer
    return margin_usd - notional * winning_close_received(bust_move, params)


def worst_case_round_loss_usd(
    margin_usd: float, leverage: int, move: float, bust_buffer: float, params: ImpactParams
) -> float:
    """Upper bound used by the daily-loss pre-check: the costlier of the two expected exits."""
    notional = margin_usd * leverage
    return max(
        expected_round_cost_usd(notional, move, params),
        liquidated_round_cost_usd(margin_usd, leverage, bust_buffer, params),
    )
