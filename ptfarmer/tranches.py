"""Budget tranches: lifetime loss budgets with availability windows and per-tranche gates.

A tranche is spent by the net losses of the rounds attributed to it (plus the
worst case of its rounds still open). Every round is attributed to exactly one
tranche when it opens. With no tranches configured, only the daily loss cap
applies.

Schedule format (JSON list, from BUDGET_TRANCHES or BUDGET_TRANCHES_FILE):
  [{"name": "today", "budget_usd": 500},
   {"name": "pre_wave", "budget_usd": 500, "available_until": "2026-10-13T02:00:00Z"},
   {"name": "reserve", "budget_usd": 300, "kind": "opportunistic"}]
Optional keys: available_from, available_until (UTC, Z suffix), min_mint_rate.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STANDARD = "standard"
OPPORTUNISTIC = "opportunistic"


class TrancheError(ValueError):
    pass


@dataclass(frozen=True)
class Tranche:
    name: str
    budget_usd: float
    kind: str = STANDARD
    available_from: float | None = None
    available_until: float | None = None
    min_mint_rate: float | None = None

    def in_window(self, now: float) -> bool:
        return (self.available_from is None or now >= self.available_from) and (
            self.available_until is None or now < self.available_until)


@dataclass(frozen=True)
class TrancheUsage:
    realized_net_usd: float = 0.0
    open_worst_usd: float = 0.0

    @property
    def used_usd(self) -> float:
        return max(0.0, -self.realized_net_usd) + self.open_worst_usd


@dataclass(frozen=True)
class OpportunisticWindow:
    lp_min_usd: float
    lp_max_usd: float

    def active(self, tracked_lp_usd: float, queue_open: bool) -> bool:
        return not queue_open and self.lp_min_usd <= tracked_lp_usd < self.lp_max_usd


@dataclass(frozen=True)
class TrancheDecision:
    tranche: Tranche | None
    detail: str


def _ts(value: str | None, field_name: str) -> float | None:
    if value in (None, ""):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise TrancheError(f"{field_name} must look like 2026-10-13T02:00:00Z, got {value!r}") from exc


def parse_tranches(data) -> tuple[Tranche, ...]:
    if not isinstance(data, list):
        raise TrancheError("tranche schedule must be a JSON list")
    out, names = [], set()
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise TrancheError(f"tranche #{i} must be an object")
        unknown = set(item) - {"name", "budget_usd", "kind", "available_from", "available_until", "min_mint_rate"}
        if unknown:
            raise TrancheError(f"tranche #{i} has unknown keys {sorted(unknown)}")
        name = str(item.get("name") or "").strip()
        if not name or name in names:
            raise TrancheError(f"tranche #{i} needs a unique name")
        names.add(name)
        try:
            budget = float(item["budget_usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise TrancheError(f"tranche {name}: budget_usd must be a number") from exc
        if budget <= 0:
            raise TrancheError(f"tranche {name}: budget_usd must be positive")
        kind = item.get("kind", STANDARD)
        if kind not in (STANDARD, OPPORTUNISTIC):
            raise TrancheError(f"tranche {name}: kind must be {STANDARD} or {OPPORTUNISTIC}")
        start, end = _ts(item.get("available_from"), "available_from"), _ts(item.get("available_until"), "available_until")
        if start is not None and end is not None and end <= start:
            raise TrancheError(f"tranche {name}: available_until must be after available_from")
        rate = item.get("min_mint_rate")
        out.append(Tranche(name, budget, kind, start, end, float(rate) if rate is not None else None))
    return tuple(out)


def load_tranches(inline: str = "", path: str = "") -> tuple[Tranche, ...]:
    if inline and path:
        raise TrancheError("set BUDGET_TRANCHES or BUDGET_TRANCHES_FILE, not both")
    if path:
        p = Path(path)
        if not p.exists():
            raise TrancheError(f"BUDGET_TRANCHES_FILE {path} does not exist")
        inline = p.read_text()
    if not inline:
        return ()
    try:
        return parse_tranches(json.loads(inline))
    except json.JSONDecodeError as exc:
        raise TrancheError(f"tranche schedule is not valid JSON: {exc}") from exc


def choose_tranche(
    tranches: tuple[Tranche, ...],
    usage: dict[str, TrancheUsage],
    now: float,
    mint_rate: float,
    default_min_mint_rate: float,
    queue_open: bool,
    opportunistic: OpportunisticWindow,
    tracked_lp_usd: float,
    min_round_worst_usd: float,
) -> TrancheDecision:
    """First standard tranche that can afford a round, else the opportunistic one if its window is active.

    `min_round_worst_usd` is the worst-case loss of a minimum-size round; the
    caller then sizes the round to the chosen tranche's remaining budget.
    """
    if queue_open:
        return TrancheDecision(None, "payout queue open")

    def ineligible(t: Tranche) -> str | None:
        min_rate = t.min_mint_rate if t.min_mint_rate is not None else default_min_mint_rate
        used = usage.get(t.name, TrancheUsage()).used_usd
        if not t.in_window(now):
            return "outside its window"
        if mint_rate < min_rate:
            return f"mint rate {mint_rate:.1f} < {min_rate:.1f}"
        if used + min_round_worst_usd > t.budget_usd:
            return f"${used:,.0f} of ${t.budget_usd:,.0f} used, a minimum round needs ${min_round_worst_usd:,.0f}"
        if t.kind == OPPORTUNISTIC and not opportunistic.active(tracked_lp_usd, queue_open):
            return (f"LP ${tracked_lp_usd / 1e6:.2f}M outside "
                    f"${opportunistic.lp_min_usd / 1e6:.1f}M-${opportunistic.lp_max_usd / 1e6:.1f}M")
        return None

    reasons = []
    for kind in (STANDARD, OPPORTUNISTIC):
        for t in (t for t in tranches if t.kind == kind):
            why = ineligible(t)
            if why is None:
                used = usage.get(t.name, TrancheUsage()).used_usd
                return TrancheDecision(t, f"{t.name}: ${used:,.0f} of ${t.budget_usd:,.0f} used")
            reasons.append(f"{t.name}: {why}")
    return TrancheDecision(None, "; ".join(reasons) or "no tranches configured")
