"""Loss accounting shared by the engine, monitor and daily summary."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from .config import Config
from .context import MarketContext
from .gates import LossBudget, worst_case_for
from .rounds import Round
from .store import Store
from .tranches import TrancheUsage


def utc_day_start(ts: float) -> float:
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def open_worst_case(cfg: Config, ctx: MarketContext | None, r: Round) -> float:
    return worst_case_for(cfg, ctx, r.market, r.margin_usd, r.leverage, r.tp_move)


def active_rounds(store: Store) -> list[Round]:
    return [Round.from_dict({**json.loads(row["data"]), "id": row["id"], "state": row["state"]}) for row in store.active_rounds()]


def daily_loss_budget(cfg: Config, store: Store, ctx: MarketContext | None, now: float, active: list[Round]) -> LossBudget:
    start, end = utc_day_start(now), utc_day_start(now) + 86400
    realized = sum(json.loads(row["data"]).get("net_pnl_usd", 0.0) for row in store.rounds_closed_between(start, end))
    realized += sum(json.loads(ev["data"]).get("net_pnl") or 0.0 for ev in store.events_between(start, end, ["sweep_closed"]))
    return LossBudget(cfg.daily_loss_cap_usd, realized, sum(open_worst_case(cfg, ctx, r) for r in active))


def tranche_usage(cfg: Config, store: Store, ctx: MarketContext | None, active: list[Round]) -> dict[str, TrancheUsage]:
    realized: dict[str, float] = {}
    for row in store.rounds_closed_between(0, float("inf")):
        data = json.loads(row["data"])
        if data.get("tranche"):
            realized[data["tranche"]] = realized.get(data["tranche"], 0.0) + data.get("net_pnl_usd", 0.0)
    open_worst: dict[str, float] = {}
    for r in active:
        if r.tranche:
            open_worst[r.tranche] = open_worst.get(r.tranche, 0.0) + open_worst_case(cfg, ctx, r)
    return {n: TrancheUsage(realized.get(n, 0.0), open_worst.get(n, 0.0)) for n in set(realized) | set(open_worst)}
