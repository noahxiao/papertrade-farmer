"""Spending pace: are realized farming losses keeping up with the schedule?

PACE_SCHEDULE is a comma-separated list of UTC_TIME=USD checkpoints, e.g.
  2026-10-12T01:00:00Z=450,2026-10-12T19:00:00Z=800
meaning "at least $450 spent by Mon 12:00 AEDT, $800 by Tue 06:00 AEDT".
Spending counts realized net losses of rounds (and orphan sweeps) closed
since PACE_START. Behind pace, the vol-matched mode closes at CLOSE_MIN.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from .store import Store


class PaceError(ValueError):
    pass


@dataclass(frozen=True)
class Checkpoint:
    at: float
    spent_usd: float


@dataclass(frozen=True)
class PaceStatus:
    spent_usd: float
    behind: bool
    due: Checkpoint | None  # latest checkpoint already passed
    next: Checkpoint | None

    def describe(self) -> str:
        parts = [f"spent ${self.spent_usd:,.0f}"]
        if self.due:
            parts.append(f"{'BEHIND' if self.behind else 'on pace'} vs ${self.due.spent_usd:,.0f} by {_fmt(self.due.at)}")
        if self.next:
            parts.append(f"next ${self.next.spent_usd:,.0f} by {_fmt(self.next.at)}")
        return "; ".join(parts)


def _fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%a %d %H:%MZ")


def parse_utc(value: str, name: str) -> float:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError as exc:
        raise PaceError(f"{name} must look like 2026-10-12T01:00:00Z, got {value!r}") from exc


def parse_schedule(text: str) -> tuple[Checkpoint, ...]:
    if not text.strip():
        return ()
    out = []
    for item in text.split(","):
        when, sep, usd = item.partition("=")
        if not sep:
            raise PaceError(f"PACE_SCHEDULE entry {item!r} must be TIME=USD")
        try:
            amount = float(usd)
        except ValueError as exc:
            raise PaceError(f"PACE_SCHEDULE amount {usd!r} is not a number") from exc
        out.append(Checkpoint(parse_utc(when, "PACE_SCHEDULE time"), amount))
    out.sort(key=lambda c: c.at)
    if any(b.spent_usd < a.spent_usd for a, b in zip(out, out[1:])):
        raise PaceError("PACE_SCHEDULE amounts must not decrease over time")
    return tuple(out)


def spent_since(store: Store, since: float) -> float:
    rows = store.rounds_closed_between(since, float("inf"))
    net = sum(json.loads(r["data"]).get("net_pnl_usd", 0.0) for r in rows)
    net += sum(json.loads(e["data"]).get("net_pnl") or 0.0 for e in store.events_between(since, float("inf"), ["sweep_closed"]))
    return max(0.0, -net)


def pace_status(schedule: tuple[Checkpoint, ...], spent_usd: float, now: float) -> PaceStatus:
    due = next((c for c in reversed(schedule) if c.at <= now), None)
    upcoming = next((c for c in schedule if c.at > now), None)
    return PaceStatus(spent_usd, due is not None and spent_usd < due.spent_usd, due, upcoming)
