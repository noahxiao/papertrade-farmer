"""Monitor/daily summary content, dry-run simulation end to end, service loop."""

from __future__ import annotations

import json

from fakes import BTC_MID, FakeClock, FakeFeeds

from ptfarmer.alerts import Notifier
from ptfarmer.app import Components
from ptfarmer.budget import utc_day_start
from ptfarmer.config import load_config
from ptfarmer.context import MarketView
from ptfarmer.engine import Farmer
from ptfarmer.gates import plan_round
from ptfarmer.guards import load_baseline
from ptfarmer.reporting import daily_summary_text, monitor_json, monitor_report, summarize_day
from ptfarmer.service import Service, heartbeat_age_s
from ptfarmer.staking import SimAutoStaker
from ptfarmer.store import Store
from ptfarmer.venue import SimVenue


def finished_round(world):
    world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.venue.land_close(world.venue.ops_of("closePositions")[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()


def test_monitor_shows_scenario_lines(world):
    ctx = world.farmer.view.refresh()
    account = world.venue.account()
    plan = plan_round(world.cfg, ctx, "BTC", account, world.farmer.breaker, world.farmer.loss_budget(ctx.now, []), None, {})
    text = monitor_report(world.cfg, ctx, world.baseline, account, plan.gates, [], {}, "daily loss budget: ...")
    for needle in ("Mint rate: 40.2 PAPER/$", "IOU payout queue: empty", "vs $2.00M flat-rate line",
                   "vs $5.00M surplus cap", "vs $10.00M surplus cap", "PAPER unstaked", "claimable rewards",
                   "payback per PAPER", "Opportunistic trigger", "daily_loss"):
        assert needle in text, needle
    data = json.loads(monitor_json(world.cfg, ctx, account, plan.gates, []))
    assert data["mint_rate"] > 40 and data["queue_open"] is False


def test_daily_summary_tracks_paper_and_budget(world):
    finished_round(world)
    day = summarize_day(world.store, utc_day_start(world.clock()))
    assert (day.opened, day.completed) == (2, 1)
    assert day.paper_minted > 0 and day.net_pnl_usd < 0
    text = daily_summary_text(world.cfg, day, {}, world.farmer.last_ctx, world.venue.account(), world.baseline, world.clock())
    for needle in ("PAPER:", "minted", "staked by the bot", "Daily loss budget:", "remaining", "mint rate",
                   "queue empty", "claimable rewards", "payback"):
        assert needle in text, needle


def test_dry_run_simulation_farms_and_auto_stakes(tmp_path):
    cfg = load_config(env={"DATA_DIR": str(tmp_path), "MAX_COST_PER_PAPER": "off", "ROUND_COOLDOWN_S": "0",
                           "STAKE_MODE": "auto", "STAKE_MIN_PAPER": "100", "SIM_FILL_DELAY_S": "5",
                           "SIM_START_BALANCE_USD": "1000"})
    clock, feeds, store = FakeClock(), FakeFeeds(), Store(tmp_path, FakeClock())
    store.clock = clock
    view = MarketView(cfg, feeds, store, load_baseline(), clock)
    sim = SimVenue(store, mids=lambda: feeds.hl_mids(), mint_rate=lambda: view.last.spot_mint_rate, clock=clock,
                   fill_delay_s=5, start_balance_usd=1000)
    farmer = Farmer(cfg, sim, view, store, Notifier("", store, clock), load_baseline(),
                    staker=SimAutoStaker(sim, store, 100), clock=clock)

    def tick(dt=10):
        clock.advance(dt)
        farmer.tick()

    tick()
    tick()
    [r] = farmer.active_rounds()
    assert r.state == "HEDGED"
    feeds.mids["BTC"] = BTC_MID * 0.9895
    tick()
    tick()
    tick()
    assert farmer.active_rounds()[0].id != r.id, "next round started"
    done = json.loads(store.db.execute("SELECT data FROM rounds WHERE id = ?", (r.id,)).fetchone()["data"])
    assert done["paper_minted"] > 0
    assert sim.state["staked"] == done["paper_minted"] and sim.state["paper"] == 0
    assert store.staked_between(0, float("inf")) == done["paper_minted"]


def test_service_heartbeat_and_clean_shutdown(world, tmp_path):
    world.open_and_hedge()
    comps = Components(world.cfg, world.store, world.feeds, world.farmer.view, world.notifier, world.baseline,
                       world.venue, world.farmer)
    service = Service(comps, clock=world.clock)
    service.install_signal_handlers = lambda: None
    service.request_shutdown()
    assert service.run() == 0
    assert heartbeat_age_s(world.cfg.heartbeat_path, world.clock()) is not None
    assert json.loads(world.cfg.heartbeat_path.read_text())["shutting_down"] is True
    assert "shutdown" in world.alert_kinds() and "shutdown_with_exposure" not in world.alert_kinds()


def test_second_instance_refuses_to_start(world):
    comps = Components(world.cfg, world.store, world.feeds, world.farmer.view, world.notifier, world.baseline,
                       world.venue, world.farmer)
    first = Service(comps, clock=world.clock)
    assert first.acquire_instance_lock()
    assert Service(comps, clock=world.clock).run() == 4
    assert world.venue.submitted == []


def test_service_grace_expiry_alerts_when_exposure_remains(make_world):
    world = make_world(SHUTDOWN_GRACE_S=0, POLL_INTERVAL_S=0)
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    comps = Components(world.cfg, world.store, world.feeds, world.farmer.view, world.notifier, world.baseline,
                       world.venue, world.farmer)
    service = Service(comps, clock=world.clock)
    service.install_signal_handlers = lambda: None
    service.request_shutdown()
    world.clock.advance(1)
    service.run()
    assert "shutdown_with_exposure" in world.alert_kinds()
    assert world.venue.ops_of("closePositions"), "it still tried to unwind before giving up"
