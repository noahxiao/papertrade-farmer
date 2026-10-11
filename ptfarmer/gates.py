"""Pre-round entry gates.

Each gate is a small pure function returning a GateResult. A failing gate
blocks *new* rounds only; open positions are always managed regardless.

  pause  clears by itself when the condition clears (queue, relayer, cost, ...)
  halt   latched until an operator runs `ptfarmer resume`
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import economics
from .config import Config
from .context import MarketContext
from .guards import RelayerBreaker
from .models import AccountSnapshot
from .tranches import OPPORTUNISTIC, OpportunisticWindow, Tranche, TrancheUsage, choose_tranche
from .value_side import payback_days


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    action: str = "pause"
    detail: str = ""
    advisory: bool = False  # economics/value gates that DRY_RUN_FORCE_ROUNDS may override


@dataclass(frozen=True)
class RoundSizing:
    margin_usd: float
    leverage: int

    @property
    def notional_usd(self) -> float:
        return self.margin_usd * self.leverage


@dataclass(frozen=True)
class LossBudget:
    cap_usd: float
    realized_today_usd: float
    open_worst_case_usd: float

    @property
    def used_usd(self) -> float:
        return max(0.0, -self.realized_today_usd)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.cap_usd - self.used_usd)


def min_margin_usd(ctx: MarketContext, leverage: int) -> float:
    """Smallest margin per leg the protocol accepts at `leverage` (rounded up to cents)."""
    return math.ceil(max(ctx.trading.min_margin_usd, ctx.trading.min_notional_usd / leverage) * 100) / 100


def size_round(cfg: Config, ctx: MarketContext, account: AccountSnapshot, target_margin_usd: float | None = None) -> RoundSizing | None:
    """Margin per leg: the target (default MARGIN_PER_LEG_USD), capped by balance when sizing from balance.

    A target of 0 means the protocol minimum at the configured leverage. None if the result is too small.
    """
    floor = min_margin_usd(ctx, cfg.leverage)
    margin = cfg.margin_per_leg_usd if target_margin_usd is None else target_margin_usd
    if margin <= 0:
        margin = floor
    if cfg.size_from_balance:
        margin = min(margin, (account.available_balance_usd - cfg.balance_buffer_usd) / 2)
    margin = int(margin * 100) / 100
    if margin < floor:
        return None
    return RoundSizing(margin, cfg.leverage)


def fit_to_room(sizing: RoundSizing | None, room_usd: float, worst_per_margin_usd: float, floor_usd: float) -> RoundSizing | None:
    """Shrink a round so its worst-case loss fits `room_usd`.

    Never below the protocol minimum: if even that does not fit, the budget
    gates see the minimum-size round and block.
    """
    if sizing is None or worst_per_margin_usd <= 0:
        return sizing
    cap = int(max(room_usd, 0.0) / worst_per_margin_usd * 100) / 100
    if cap >= sizing.margin_usd:
        return sizing
    return RoundSizing(max(cap, floor_usd), sizing.leverage)


def _g(name: str, ok: bool, detail: str = "", action: str = "pause", advisory: bool = False) -> GateResult:
    return GateResult(name, ok, action, detail, advisory)


def market_open_gate(ctx: MarketContext, market: str) -> GateResult:
    inst = ctx.trading.instruments.get(market)
    if ctx.trading.trading_paused:
        return _g("market_open", False, "trading is paused protocol-wide")
    if inst is None or not inst.openable:
        return _g("market_open", False, f"{market} is not openable")
    blocked = {"openPosition", "closePositions"} & ctx.trading.paused_actions
    if blocked:
        return _g("market_open", False, f"relayer intake paused for {sorted(blocked)}")
    return _g("market_open", True)


def payout_queue_gate(ctx: MarketContext) -> GateResult:
    if ctx.queue.is_open:
        return _g("payout_queue", False, f"IOU queue open: ${ctx.queue.total_usd:,.0f} across {ctx.queue.count} entries")
    return _g("payout_queue", True)


def relayer_gate(ctx: MarketContext, breaker: RelayerBreaker) -> GateResult:
    if not ctx.relayer_ok:
        return _g("relayer", False, "/relayer/health not ready")
    if breaker.is_degraded:
        return _g("relayer", False, f"{breaker.failures} consecutive relayer failures")
    if not breaker.can_submit():
        return _g("relayer", False, "relayer backoff in effect")
    return _g("relayer", True)


def size_gate(ctx: MarketContext, market: str, sizing: RoundSizing | None) -> GateResult:
    if sizing is None:
        return _g("size", False, "available balance too small for a minimum-size pair plus buffer")
    inst = ctx.trading.instruments[market]
    if sizing.notional_usd < ctx.trading.min_notional_usd:
        return _g("size", False, f"notional ${sizing.notional_usd:,.0f} below minimum ${ctx.trading.min_notional_usd:,.0f}")
    if sizing.notional_usd > inst.max_position_usd or sizing.leverage > inst.max_leverage:
        return _g("size", False, "position exceeds market size/leverage limit")
    mid = ctx.mids.get(market)
    if mid:
        coins = sizing.notional_usd / mid
        if inst.long_oi + coins > inst.long_cap or inst.short_oi + coins > inst.short_cap:
            return _g("size", False, "open-interest cap headroom insufficient")
    return _g("size", True, f"${sizing.margin_usd:,.2f} margin per leg at {sizing.leverage}x (${sizing.notional_usd:,.0f} notional)")


def cost_gate(cfg: Config, ctx: MarketContext, market: str) -> GateResult:
    est = ctx.costs[market]
    detail = (f"est. ${est.cost_per_paper:.5f}/PAPER at close (spot ${est.spot.cost_per_paper:.5f}, "
              f"rate {est.spot.mint_rate:.1f}->{est.at_close.mint_rate:.1f} over {est.cycle_hours:.1f}h)")
    if cfg.max_cost_per_paper is None:
        return _g("cost_per_paper", True, detail + "; gate disabled")
    ok = est.cost_per_paper <= cfg.max_cost_per_paper
    return _g("cost_per_paper", ok, detail + f" vs limit ${cfg.max_cost_per_paper:.5f}", advisory=True)


def daily_loss_gate(budget: LossBudget, next_round_worst_usd: float) -> GateResult:
    projected = budget.used_usd + budget.open_worst_case_usd + next_round_worst_usd
    detail = (f"used ${budget.used_usd:,.2f} of ${budget.cap_usd:,.2f}; open worst-case ${budget.open_worst_case_usd:,.2f}; "
              f"next round worst-case ${next_round_worst_usd:,.2f}")
    return _g("daily_loss", projected <= budget.cap_usd, detail)


def session_key_gate(cfg: Config, account: AccountSnapshot, session_address: str | None, now: float) -> GateResult:
    if not cfg.live or session_address is None:
        return _g("session_key", True)
    expiry = account.session_keys.get(session_address.lower())
    if expiry is None:
        return _g("session_key", False, "session key is not registered for this wallet")
    if expiry < now + 2 * 3600:
        return _g("session_key", False, "session key expires within 2h")
    return _g("session_key", True)


def value_side_gates(cfg: Config, ctx: MarketContext, market: str) -> list[GateResult]:
    v = ctx.value
    out = []
    if cfg.min_fee_yield_per_paper_day is not None:
        y = v.fee_yield_per_paper_day
        out.append(_g("fee_yield", y is not None and y >= cfg.min_fee_yield_per_paper_day,
                      f"yield {y if y is not None else 'n/a'} $/PAPER/day vs min {cfg.min_fee_yield_per_paper_day}", advisory=True))
    if cfg.max_payback_days is not None:
        days = payback_days(ctx.costs[market].cost_per_paper, v.fee_yield_per_paper_day, v.yield_trend_per_day)
        out.append(_g("payback", days is not None and days <= cfg.max_payback_days,
                      f"payback {f'{days:.0f}d' if days is not None else 'never (365d horizon)'} vs max {cfg.max_payback_days:.0f}d", advisory=True))
    if cfg.min_lp_fraction_of_cap is not None:
        out.append(_g("lp_vs_cap", v.lp_fraction_of_cap >= cfg.min_lp_fraction_of_cap,
                      f"LP {v.lp_fraction_of_cap:.0%} of ${v.active_cap_usd:,.0f} cap vs min {cfg.min_lp_fraction_of_cap:.0%}", advisory=True))
    if cfg.require_lp_trend_nonnegative:
        t = v.lp_trend_usd_per_h
        out.append(_g("lp_trend", t is not None and t >= 0, f"LP trend {t if t is not None else 'n/a'} $/h", advisory=True))
    return out


def lp_floor_gate(cfg: Config, ctx: MarketContext) -> GateResult:
    if cfg.lp_floor_usd is None:
        return _g("lp_floor", True)
    lp, trend = ctx.value.lp_usd, ctx.value.lp_trend_usd_per_h
    if lp < cfg.lp_floor_usd:
        return _g("lp_floor", False, f"tracked LP ${lp / 1e6:.2f}M below ${cfg.lp_floor_usd / 1e6:.2f}M floor")
    if trend is not None and trend < 0:
        projected = lp + trend * cfg.lp_trend_horizon_h
        if projected < cfg.lp_floor_usd:
            return _g("lp_floor", False, f"LP trending below floor: ${lp / 1e6:.2f}M, {trend / 1e3:+.0f}k/h, "
                                         f"${projected / 1e6:.2f}M in {cfg.lp_trend_horizon_h:g}h")
    return _g("lp_floor", True)


def mint_rate_stop_gate(cfg: Config, ctx: MarketContext) -> GateResult:
    if cfg.mint_rate_stop is None:
        return _g("mint_rate_stop", True)
    rate = ctx.spot_mint_rate
    if rate < cfg.mint_rate_stop and ctx.value.excess_usd <= 0:
        return _g("mint_rate_stop", False, f"mint rate {rate:.1f} < {cfg.mint_rate_stop:g} and no surplus flowing to stakers")
    return _g("mint_rate_stop", True)


def close_latency_gate(paused_until: float, now: float) -> GateResult:
    if now < paused_until:
        return _g("close_latency", False, f"closes repeatedly slow; paused for {(paused_until - now) / 60:.0f} more min")
    return _g("close_latency", True)


@dataclass(frozen=True)
class RoundPlan:
    sizing: RoundSizing | None
    tranche: Tranche | None
    gates: list[GateResult]


def _worst_case(cfg: Config, ctx: MarketContext, market: str, sizing: RoundSizing | None) -> float:
    inst = ctx.trading.instruments.get(market)
    if sizing is None or inst is None:
        return 0.0
    return cfg.round_cost_safety_factor * economics.worst_case_round_loss_usd(
        sizing.margin_usd, sizing.leverage, cfg.take_profit_move, inst.bust_buffer, economics.ImpactParams.from_instrument(inst))


def plan_round(cfg: Config, ctx: MarketContext, market: str, account: AccountSnapshot, breaker: RelayerBreaker,
               budget: LossBudget, session_address: str | None, tranche_usage: dict[str, TrancheUsage],
               close_latency_paused_until: float = 0.0) -> RoundPlan:
    """Pick tranche and size for the next round and evaluate every entry gate.

    The round is sized down so that its worst case fits the chosen tranche and
    the daily loss cap, which lets budgets be spent fully without being overrun.
    """
    sizing = size_round(cfg, ctx, account)
    floor = min_margin_usd(ctx, cfg.leverage)
    worst_per_margin = _worst_case(cfg, ctx, market, RoundSizing(1.0, cfg.leverage))
    tranche = None
    results = [
        market_open_gate(ctx, market),
        payout_queue_gate(ctx),
        relayer_gate(ctx, breaker),
        session_key_gate(cfg, account, session_address, ctx.now),
        lp_floor_gate(cfg, ctx),
        mint_rate_stop_gate(cfg, ctx),
        close_latency_gate(close_latency_paused_until, ctx.now),
    ]
    if cfg.tranches:
        decision = choose_tranche(
            cfg.tranches, tranche_usage, ctx.now, ctx.spot_mint_rate, cfg.min_mint_rate, ctx.queue.is_open,
            OpportunisticWindow(cfg.opportunistic_lp_min_usd, ctx.summary.mint.cliff_usd), ctx.value.lp_usd,
            worst_per_margin * floor,
        )
        tranche = decision.tranche
        if tranche is not None:
            if tranche.kind == OPPORTUNISTIC:
                sizing = size_round(cfg, ctx, account, cfg.opportunistic_margin_per_leg_usd)
            room = tranche.budget_usd - tranche_usage.get(tranche.name, TrancheUsage()).used_usd
            sizing = fit_to_room(sizing, room, worst_per_margin, floor)
        results.append(_g("tranche", tranche is not None, decision.detail))
    daily_room = budget.cap_usd - budget.used_usd - budget.open_worst_case_usd
    sizing = fit_to_room(sizing, daily_room, worst_per_margin, floor)
    if market in ctx.trading.instruments:
        results += [size_gate(ctx, market, sizing), cost_gate(cfg, ctx, market),
                    daily_loss_gate(budget, _worst_case(cfg, ctx, market, sizing))]
        results += value_side_gates(cfg, ctx, market)
    return RoundPlan(sizing, tranche, results)


def worst_case_for(cfg: Config, ctx: MarketContext | None, market: str, margin_usd: float, leverage: int, tp_move: float) -> float:
    inst = ctx.trading.instruments.get(market) if ctx else None
    params = economics.ImpactParams.from_instrument(inst) if inst else economics.BTC_LAUNCH_PARAMS
    buffer = inst.bust_buffer if inst else 0.000476
    return cfg.round_cost_safety_factor * economics.worst_case_round_loss_usd(margin_usd, leverage, tp_move, buffer, params)


def blocking(results: list[GateResult], force_advisory: bool = False) -> list[GateResult]:
    return [r for r in results if not r.ok and not (force_advisory and r.advisory)]
