from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fakes import FakeClock, FakeFeeds, MockVenue  # noqa: E402
from ptfarmer import economics  # noqa: E402
from ptfarmer.alerts import Notifier  # noqa: E402
from ptfarmer.config import load_config  # noqa: E402
from ptfarmer.context import MarketView  # noqa: E402
from ptfarmer.engine import Farmer  # noqa: E402
from ptfarmer.guards import load_baseline  # noqa: E402
from ptfarmer.rounds import Round  # noqa: E402
from ptfarmer.staking import NoStaking  # noqa: E402
from ptfarmer.store import Store  # noqa: E402

BASE_ENV = {"MAX_COST_PER_PAPER": "off", "ROUND_COOLDOWN_S": "0", "STAKE_MODE": "off"}


class World:
    """One farmer wired to fake feeds and a scripted venue, sharing a fake clock."""

    def __init__(self, tmp_path: Path, **env):
        self.tmp_path = tmp_path
        self.env = {**BASE_ENV, "DATA_DIR": str(tmp_path / "data"), **{k: str(v) for k, v in env.items()}}
        self.cfg = load_config(env=self.env)
        self.clock = FakeClock()
        self.feeds = FakeFeeds()
        s = self.feeds.summary_obj
        self.venue = MockVenue(self.clock, mint_rate=economics.mint_rate(
            s.tail_progress_usd, s.mint.flat_rate, s.mint.decay_scale_usd, s.tracked_lp_usd, s.mint.cliff_usd))
        self.baseline = load_baseline()
        self.staker = NoStaking()
        self.farmer = self.new_farmer()

    def new_farmer(self) -> Farmer:
        """A fresh process over the same database and venue: what a restart looks like."""
        self.store = Store(self.cfg.data_dir, self.clock)
        self.notifier = Notifier("", self.store, self.clock)
        view = MarketView(self.cfg, self.feeds, self.store, self.baseline, self.clock)
        self.farmer = Farmer(self.cfg, self.venue, view, self.store, self.notifier, self.baseline,
                             staker=self.staker, clock=self.clock)
        return self.farmer

    def tick(self, advance_s: float = 10) -> None:
        self.clock.advance(advance_s)
        self.farmer.tick()

    def active(self) -> list[Round]:
        return self.farmer.active_rounds()

    def only_round(self) -> Round:
        rounds = self.active()
        assert len(rounds) == 1, rounds
        return rounds[0]

    def round(self, round_id: int) -> Round:
        row = self.store.db.execute("SELECT id, state, data FROM rounds WHERE id = ?", (round_id,)).fetchone()
        return Round.from_dict({**json.loads(row["data"]), "id": row["id"], "state": row["state"]})

    def events(self, kind: str) -> list[dict]:
        return [json.loads(e["data"]) for e in self.store.events_between(0, float("inf"), [kind])]

    def alert_kinds(self) -> list[str]:
        return [kind for kind, _ in self.notifier.sent]

    def open_and_hedge(self) -> Round:
        """Start a round (unless one is already opening) and fill both legs at the current mid."""
        if not self.active():
            self.tick()
        r = self.only_round()
        self.venue.fill(r.long.intent_id, self.feeds.mids["BTC"])
        self.venue.fill(r.short.intent_id, self.feeds.mids["BTC"])
        self.tick()
        r = self.round(r.id)
        assert r.state == "HEDGED"
        return r

    def naked_exposure(self) -> float:
        """Signed BTC notional held right now (0 when flat or perfectly hedged)."""
        return sum((1 if p.is_long else -1) * p.margin_usd * p.leverage for p in self.venue.positions.values())


@pytest.fixture
def make_world(tmp_path):
    def factory(**env) -> World:
        return World(tmp_path, **env)
    return factory


@pytest.fixture
def world(make_world) -> World:
    return make_world()
