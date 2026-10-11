"""Plain-text (and JSON) monitor report and daily summary."""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone

from .config import Config
from .context import MarketContext
from .gates import GateResult
from .guards import Finding
from .models import AccountSnapshot
from .store import Store
from .tranches import OPPORTUNISTIC, OpportunisticWindow, TrancheUsage, effective_budget, tranche_room
from .value_side import YIELD_WINDOW_H, payback_days
from .volatility import VOL_MATCHED, CloseShape, VolReading, choose_shape


def _utc(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _m(usd: float) -> str:
    return f"${usd / 1e6:,.2f}M"


def _opt(value, fmt: str, none: str = "n/a") -> str:
    return none if value is None else format(value, fmt)


def lp_lines(ctx: MarketContext, baseline: dict) -> list[tuple[str, float, float]]:
    """(label, line, LP minus line) for the flat-rate cliff and every scheduled surplus cap."""
    lp = ctx.summary.tracked_lp_usd
    lines = [("flat-rate line", ctx.summary.mint.cliff_usd)]
    for entry in baseline["staker_cap_schedule"]:
        lines.append((f"surplus cap from {entry['from'][:16]}Z", entry["cap_usd"]))
    seen, out = set(), []
    for label, value in lines:
        if value in seen:
            continue
        seen.add(value)
        out.append((label, value, lp - value))
    return out


def protocol_section(cfg: Config, ctx: MarketContext, baseline: dict) -> list[str]:
    s, v = ctx.summary, ctx.value
    region = "flat region (LP below cliff)" if ctx.in_flat_region else f"ratchet, tail progress {_m(s.tail_progress_usd)}"
    est = ctx.costs.get("BTC") or next(iter(ctx.costs.values()))
    lines = [
        f"  trading {'PAUSED' if ctx.trading.trading_paused else 'open'} | relayer {'healthy' if ctx.relayer_ok else 'NOT READY'}"
        f" | intake paused: {', '.join(sorted(ctx.trading.paused_actions)) or 'none'}",
        f"  Mint rate: {ctx.spot_mint_rate:.1f} PAPER/$ ({region})",
        f"  Rate decay: tail +{_m(ctx.tail_growth_usd_per_h)}/h ({ctx.tail_growth_source}) -> {est.rate_decay_per_hour:.1%}/h",
        f"  IOU payout queue: {'OPEN' if ctx.queue.is_open else 'empty'} (${ctx.queue.total_usd:,.0f}, {ctx.queue.count} entries)",
        f"  Tracked LP: {_m(s.tracked_lp_usd)}; trend {_opt(v.lp_trend_usd_per_h and v.lp_trend_usd_per_h / 1e3, '+,.0f')}k/h ({v.lp_trend_source})",
    ]
    for label, line, diff in lp_lines(ctx, baseline):
        lines.append(f"    vs {_m(line)} {label}: {'+' if diff >= 0 else '-'}{_m(abs(diff))}")
    lines.append(f"  Active surplus cap: {_m(v.active_cap_usd)} (LP at {v.lp_fraction_of_cap:.0%}); surplus to stakers now ${v.excess_usd:,.0f}")
    if v.next_cap_change:
        lines.append(f"  Next cap change: {_m(v.next_cap_change[1])} at {_utc(v.next_cap_change[0])}")
    lines.append(f"  Staker rewards (last {YIELD_WINDOW_H}h): {_opt(v.staker_rewards_usd_per_day, ',.0f')} $/day = "
                 f"${_opt(v.fee_yield_per_paper_day, '.6f')} per PAPER per day; trend {_opt(v.yield_trend_per_day, '+.0%')}/day")
    lines.append(f"  PAPER staked supply: {s.staked_paper / 1e9:.3f}B of {s.supply_paper / 1e9:.3f}B")
    return lines


def shape_lines(market: str, shape: CloseShape) -> list[str]:
    lines = [f"  {market} {shape.leverage}x, close at {shape.tp_move:.2%} ({shape.mode}): liquidation after "
             f"{shape.liq_move:.3%} adverse (take-profit = {shape.tp_fraction_of_liq:.0%} of it, gap {shape.gap_to_liq:.3%}); "
             f"emergency close {shape.emergency_distance:.3%} before bust"]
    if shape.mode == VOL_MATCHED:
        lines.append(f"    sigma {_opt(shape.sigma_hourly, '.3%')}/h ({shape.sigma_source}); {shape.note}")
    return lines


def spend_rate_usd_per_h(cfg: Config, ctx: MarketContext, market: str) -> float | None:
    """Expected farming loss per hour at the current shape: cost of one round / expected round length."""
    shape, est = ctx.shapes.get(market), ctx.costs.get(market)
    if shape is None or est is None or est.cycle_hours <= 0:
        return None
    return cfg.margin_per_leg_usd * shape.leverage * est.spot.net_cost_per_notional / est.cycle_hours


def pace_line(cfg: Config, ctx: MarketContext) -> str | None:
    if not ctx.pace:
        return None
    line = f"  Pace: {ctx.pace.describe()}"
    rate = sum(r for m in cfg.markets if (r := spend_rate_usd_per_h(cfg, ctx, m)) is not None)
    nxt = ctx.pace.next
    if rate and nxt:
        projected = ctx.pace.spent_usd + rate * max(nxt.at - ctx.now, 0) / 3600
        line += f"; at ~${rate:,.0f}/h projected ${projected:,.0f} by then"
    return line


def economics_section(cfg: Config, ctx: MarketContext, close_quality: dict | None = None) -> list[str]:
    lines = []
    for market in cfg.markets:
        est = ctx.costs[market]
        inst = ctx.trading.instruments[market]
        shape = ctx.shapes.get(market) or choose_shape(cfg, market, VolReading(None, "not_used"), inst.bust_buffer,
                                                       inst.max_leverage)
        pb_flat = payback_days(est.cost_per_paper, ctx.value.fee_yield_per_paper_day, 0.0)
        pb_trend = payback_days(est.cost_per_paper, ctx.value.fee_yield_per_paper_day, ctx.value.yield_trend_per_day)
        lines += shape_lines(market, shape) + [
            f"    cost/PAPER spot ${est.spot.cost_per_paper:.5f}; at close ${est.cost_per_paper:.5f} "
            f"(cycle ~{est.cycle_hours:.1f}h, rate {est.spot.mint_rate:.1f} -> {est.at_close.mint_rate:.1f}); "
            f"limit {_opt(cfg.max_cost_per_paper, '.5f', 'off')}",
            f"    at ${cfg.margin_per_leg_usd:,.0f}/leg: ~${cfg.margin_per_leg_usd * shape.leverage * est.spot.net_cost_per_notional:,.0f} "
            f"expected loss per round, ~${_opt(spend_rate_usd_per_h(cfg, ctx, market), ',.0f')}/h",
            f"    payback per PAPER: {_opt(pb_flat, '.1f', 'never')} days at current yield, "
            f"{_opt(pb_trend, '.1f', 'never')} days with yield trend (365d horizon)",
            f"    mid {ctx.mids.get(market, float('nan')):,.2f}; OI long {inst.long_oi:,.0f}/{inst.long_cap:,.0f}, "
            f"short {inst.short_oi:,.0f}/{inst.short_cap:,.0f}",
        ]
    window = OpportunisticWindow(cfg.opportunistic_lp_min_usd, ctx.summary.mint.cliff_usd)
    lines.append(f"  Opportunistic trigger (LP {_m(window.lp_min_usd)}-{_m(window.lp_max_usd)}, queue empty): "
                 f"{'ACTIVE' if window.active(ctx.summary.tracked_lp_usd, ctx.queue.is_open) else 'inactive'}")
    if pace_line(cfg, ctx):
        lines.append(pace_line(cfg, ctx))
    q = close_quality or {}
    if q.get("derate_steps") or q.get("slow_streak"):
        lines.append(f"  Close quality: leverage derate {q.get('derate_steps', 0)}/{cfg.leverage_derate_max_steps} step(s); "
                     f"{q.get('slow_streak', 0)} slow/overshooting close(s) in a row")
    return lines


def account_section(account: AccountSnapshot | None, ctx: MarketContext) -> list[str]:
    if account is None:
        return ["  (no WALLET_ADDRESS configured)"]
    return [
        f"  available ${account.available_balance_usd:,.2f}; queued ${account.queued_usd:,.2f}; open positions {len(account.positions)}; "
        f"pending intents {len(account.operations)}",
        f"  PAPER unstaked {account.paper_balance:,.0f}; staked {account.paper_staked:,.0f}; "
        f"claimable rewards ${account.claimable_rewards_usd(ctx.trading.acc_reward_per_share_raw):,.2f}",
    ]


def tranche_section(cfg: Config, usage: dict[str, TrancheUsage], now: float) -> list[str]:
    if not cfg.tranches:
        return ["  (none configured; daily loss cap only)"]
    lines = []
    for t in cfg.tranches:
        u = usage.get(t.name, TrancheUsage())
        window = []
        if t.available_from:
            window.append(f"from {_utc(t.available_from)}")
        if t.available_until:
            window.append(f"until {_utc(t.available_until)}")
        state = "open" if t.in_window(now) else "closed"
        rate = t.min_mint_rate if t.min_mint_rate is not None else cfg.min_mint_rate
        budget = effective_budget(t, cfg.tranches, usage)
        lines.append(f"  {t.name} ({t.kind}{', LP-band trigger' if t.kind == OPPORTUNISTIC else ''}"
                     f"{', unspent remainder of the plan' if t.remainder else ''}): "
                     f"${u.used_usd:,.2f} of ${budget:,.2f} used, ${tranche_room(t, cfg.tranches, usage):,.2f} left; "
                     f"min mint rate {rate:g}; window {state} {' '.join(window)}".rstrip())
    return lines


def gates_section(gates: list[GateResult], findings: list[Finding]) -> list[str]:
    lines = [f"  [{'ok' if g.ok else 'BLOCK'}] {g.name}{': ' + g.detail if g.detail else ''}" for g in gates]
    if findings:
        lines += [f"  [{f.severity.upper()}] rule {f.check}: {f.message}" for f in findings]
    else:
        lines.append("  [ok] rule check: protocol parameters match the pinned baseline")
    return lines


def monitor_report(cfg: Config, ctx: MarketContext, baseline: dict, account: AccountSnapshot | None,
                   gates: list[GateResult], findings: list[Finding], usage: dict[str, TrancheUsage],
                   budget_line: str, close_quality: dict | None = None) -> str:
    out = [f"Papertrade monitor  {_utc(ctx.now)}  block {ctx.summary.block:,}  mode {cfg.mode} (read-only)", "Protocol"]
    out += protocol_section(cfg, ctx, baseline)
    out += ["Economics"] + economics_section(cfg, ctx, close_quality)
    out += ["Account"] + account_section(account, ctx)
    out += ["Budget", f"  {budget_line}"] + tranche_section(cfg, usage, ctx.now)
    out += ["Next-round gates"] + gates_section(gates, findings)
    return "\n".join(out)


def monitor_json(cfg: Config, ctx: MarketContext, account: AccountSnapshot | None, gates: list[GateResult],
                 findings: list[Finding]) -> str:
    v = ctx.value
    return json.dumps({
        "ts": ctx.now, "block": ctx.summary.block, "mint_rate": ctx.spot_mint_rate, "flat_region": ctx.in_flat_region,
        "tracked_lp_usd": v.lp_usd, "active_cap_usd": v.active_cap_usd, "excess_usd": v.excess_usd,
        "lp_trend_usd_per_h": v.lp_trend_usd_per_h, "queue_open": v.queue_open, "queue_usd": v.queue_usd,
        "staker_rewards_usd_per_day": v.staker_rewards_usd_per_day, "yield_per_paper_day": v.fee_yield_per_paper_day,
        "cost_per_paper": {m: {"spot": c.spot.cost_per_paper, "at_close": c.cost_per_paper, "cycle_h": c.cycle_hours}
                           for m, c in ctx.costs.items()},
        "payback_days": {m: payback_days(c.cost_per_paper, v.fee_yield_per_paper_day, v.yield_trend_per_day)
                         for m, c in ctx.costs.items()},
        "close_shape": {m: {"mode": s.mode, "tp_move": s.tp_move, "leverage": s.leverage, "liq_move": s.liq_move,
                            "tp_fraction_of_liq": s.tp_fraction_of_liq, "emergency_distance": s.emergency_distance,
                            "sigma_hourly": s.sigma_hourly, "sigma_source": s.sigma_source, "behind_pace": s.behind_pace,
                            "derate_steps": s.derate_steps, "note": s.note,
                            "spend_usd_per_h": spend_rate_usd_per_h(cfg, ctx, m)}
                        for m, s in ctx.shapes.items() if m in cfg.markets},
        "pace": None if ctx.pace is None else {"spent_usd": ctx.pace.spent_usd, "behind": ctx.pace.behind,
                                               "describe": ctx.pace.describe()},
        "account": None if account is None else {
            "available_usd": account.available_balance_usd, "paper_unstaked": account.paper_balance,
            "paper_staked": account.paper_staked,
            "claimable_rewards_usd": account.claimable_rewards_usd(ctx.trading.acc_reward_per_share_raw)},
        "gates": [g.__dict__ for g in gates], "rule_findings": [f.__dict__ for f in findings],
    }, indent=2)


@dataclass(frozen=True)
class DaySummary:
    day: str
    opened: int
    completed: int
    unwound: int
    unfilled: int
    liquidations: int
    net_pnl_usd: float
    paper_minted: float
    paper_staked: float
    cost_per_paper: float | None
    expected_cost_per_paper: float | None
    close_latencies_s: list[float]
    sweep_pnl_usd: float
    alerts: dict[str, int]


def summarize_day(store: Store, day_start: float) -> DaySummary:
    end = day_start + 86400
    closed = [json.loads(r["data"]) | {"state": r["state"]} for r in store.rounds_closed_between(day_start, end)]
    opened = len(store.rounds_created_between(day_start, end))
    completed = [r for r in closed if r["state"] == "DONE"]
    aborted = [r for r in closed if r["state"] == "ABORTED"]
    unwound = [r for r in aborted if any(r[side].get("position_id") for side in ("long", "short"))]
    sweep = sum(json.loads(e["data"]).get("net_pnl") or 0.0 for e in store.events_between(day_start, end, ["sweep_closed"]))
    alerts: dict[str, int] = {}
    for e in store.events_between(day_start, end, ["alert"]):
        kind = json.loads(e["data"]).get("alert_kind", "?")
        alerts[kind] = alerts.get(kind, 0) + 1
    net = sum(r.get("net_pnl_usd", 0.0) for r in closed) + sweep
    paper = sum(r.get("paper_minted", 0.0) for r in closed)
    expected = [r["expected_cost_per_paper"] for r in completed if r.get("expected_cost_per_paper")]
    return DaySummary(
        day=datetime.fromtimestamp(day_start, tz=timezone.utc).strftime("%Y-%m-%d"),
        opened=opened, completed=len(completed), unwound=len(unwound), unfilled=len(aborted) - len(unwound),
        liquidations=sum(1 for r in closed if r.get("liquidated")),
        net_pnl_usd=net, paper_minted=paper, paper_staked=store.staked_between(day_start, end),
        cost_per_paper=-net / paper if paper > 0 else None,
        expected_cost_per_paper=statistics.mean(expected) if expected else None,
        close_latencies_s=[r["close_latency_s"] for r in closed if r.get("close_latency_s")],
        sweep_pnl_usd=sweep, alerts=alerts,
    )


def daily_summary_text(cfg: Config, s: DaySummary, usage: dict[str, TrancheUsage], ctx: MarketContext | None,
                       account: AccountSnapshot | None, baseline: dict, now: float) -> str:
    used = max(0.0, -s.net_pnl_usd)
    lines = [
        f"Papertrade farmer daily summary {s.day} (UTC), mode {cfg.mode}",
        f"Rounds: {s.opened} opened, {s.completed} completed hedged, {s.unwound} unwound (one leg), "
        f"{s.unfilled} never filled; {s.liquidations} losing-leg liquidations",
        f"PnL: ${s.net_pnl_usd:+,.2f} net (incl. orphan sweeps ${s.sweep_pnl_usd:+,.2f})",
        f"PAPER: {s.paper_minted:,.0f} minted, {s.paper_staked:,.0f} staked by the bot",
        f"Cost per PAPER: realized {_opt(s.cost_per_paper, '.5f', 'n/a')} vs expected {_opt(s.expected_cost_per_paper, '.5f', 'n/a')}",
        f"Daily loss budget: ${used:,.2f} used of ${cfg.daily_loss_cap_usd:,.2f}, ${max(cfg.daily_loss_cap_usd - used, 0):,.2f} remaining",
    ]
    if cfg.tranches:
        lines.append("Tranches:")
        lines += tranche_section(cfg, usage, now)
    if s.close_latencies_s:
        lat = s.close_latencies_s
        slow = sum(1 for x in lat if x > cfg.close_latency_alert_s)
        lines.append(f"Close latency: median {statistics.median(lat) / 60:.1f} min, max {max(lat) / 60:.1f} min, "
                     f"{slow} slower than {cfg.close_latency_alert_s / 60:.0f} min")
    if ctx is not None:
        v = ctx.value
        lp_vs = ", ".join(f"{'+' if d >= 0 else '-'}{_m(abs(d))} vs {_m(line)}" for _, line, d in lp_lines(ctx, baseline))
        btc = ctx.costs.get("BTC")
        pb = payback_days(btc.cost_per_paper, v.fee_yield_per_paper_day, v.yield_trend_per_day) if btc else None
        for market in cfg.markets:
            if market in ctx.shapes:
                lines += [line.strip() for line in shape_lines(f"Next round {market}:", ctx.shapes[market])]
        if ctx.pace:
            lines.append(pace_line(cfg, ctx).strip())
        lines += [
            f"Protocol now: mint rate {ctx.spot_mint_rate:.1f}/$; LP {_m(v.lp_usd)} ({lp_vs}); "
            f"queue {'OPEN $' + format(v.queue_usd, ',.0f') if v.queue_open else 'empty'}",
            f"Staker yield ${_opt(v.fee_yield_per_paper_day, '.6f')}/PAPER/day; est. payback {_opt(pb, '.1f', 'never')} days",
        ]
    if account is not None and ctx is not None:
        lines.append(f"Account: PAPER staked {account.paper_staked:,.0f}, unstaked {account.paper_balance:,.0f}, "
                     f"claimable rewards ${account.claimable_rewards_usd(ctx.trading.acc_reward_per_share_raw):,.2f}, "
                     f"available ${account.available_balance_usd:,.2f}")
    if s.alerts:
        lines.append("Alerts: " + ", ".join(f"{k} x{n}" for k, n in sorted(s.alerts.items())))
    return "\n".join(lines)
