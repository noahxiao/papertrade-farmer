"""Stops, pauses, restarts and shutdown."""

from __future__ import annotations

import json

from fakes import make_summary, make_trading

from ptfarmer import economics


def close_ops(world):
    return world.venue.ops_of("closePositions")


# ------------------------------------------------------------------ relayer
def test_relayer_health_down_blocks_new_rounds_and_alerts(world):
    world.feeds.relayer_ok = False
    world.tick()
    assert world.active() == [] and world.venue.submitted == []
    assert "relayer_unavailable" in world.alert_kinds()

    world.tick(world.cfg.relayer_down_halt_s)
    assert "relayer_down" in world.alert_kinds()

    world.feeds.relayer_ok = True
    world.tick()
    assert "gate_cleared" in world.alert_kinds()
    assert len(world.active()) == 1


def test_transport_failure_on_first_leg_never_sends_second_and_backs_off(world):
    world.venue.down = True
    world.tick()
    r = world.only_round()
    assert len(world.venue.submitted_of("openPosition")) == 1
    assert (r.long.submit_status, r.short.status) == ("unconfirmed", "dead")
    assert world.farmer.breaker.failures == 1 and not world.farmer.breaker.can_submit()


def test_unconfirmed_leg_that_lands_during_outage_is_closed_after_recovery(world):
    world.venue.down = True
    world.venue.unconfirmed_lands = True
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)  # "timed out" request had in fact been queued
    world.tick()
    assert world.round(r.id).state == "CLOSING"

    world.venue.down = False
    world.tick(600)
    r = world.round(r.id)
    [close] = close_ops(world)
    assert close["position_ids"] == [r.long.position_id]
    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "ABORTED" and world.venue.positions == {}


def test_relayer_outage_while_closing_retries_with_backoff_then_recovers(world):
    r = world.open_and_hedge()
    world.venue.down = True
    world.feeds.move(+1.05)
    for _ in range(10):
        world.tick(400)
    assert world.farmer.breaker.is_degraded
    assert "relayer_down" in world.alert_kinds()
    assert world.round(r.id).state == "CLOSING"

    world.venue.down = False
    world.tick(400)
    [close] = close_ops(world)
    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"
    assert world.farmer.breaker.failures == 0


# ---------------------------------------------------------------- payout queue
def test_iou_queue_open_pauses_new_rounds_but_open_rounds_still_close(world):
    r = world.open_and_hedge()
    world.feeds.set_queue(25_000, 4)
    world.feeds.move(+1.05)
    world.tick()
    assert world.round(r.id).state == "CLOSING", "management continues while paused"
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"
    assert world.active() == []
    assert "iou_queue_open" in world.alert_kinds()

    world.feeds.set_queue(0, 0)
    world.tick()
    assert len(world.active()) == 1


# ---------------------------------------------------------------- economics
def test_cost_gate_trips_at_default_limit(make_world):
    world = make_world(MAX_COST_PER_PAPER="0.0016")
    world.tick()
    assert world.active() == []
    assert "cost_gate" in world.alert_kinds()
    assert "cost_per_paper" in world.farmer.status()["blocked"]["BTC"]


def test_cost_gate_off_and_dry_run_force(make_world):
    assert make_world(MAX_COST_PER_PAPER="off").cfg.max_cost_per_paper is None
    world = make_world(MAX_COST_PER_PAPER="0.0016", DRY_RUN_FORCE_ROUNDS="true")
    world.tick()
    assert len(world.active()) == 1


def test_daily_loss_cap_blocks_rounds_whose_worst_case_does_not_fit(make_world):
    world = make_world(DAILY_LOSS_CAP_USD=50)
    world.tick()
    assert world.active() == []
    assert "daily_loss_cap" in world.alert_kinds()


def test_mint_rate_below_model_halts(world):
    world.venue.mint_rate = 20.0
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"
    assert world.farmer.halted()["reason"] == "mint_curve_mismatch"
    assert world.active() == []


# ----------------------------------------------------------------- rule watch
def test_parameter_change_halts_until_resume(world):
    r = world.open_and_hedge()
    world.feeds.trading_obj = make_trading(rate_multiplier=20_000.0)
    world.tick()
    assert world.farmer.halted()["reason"] == "rule_change"
    world.feeds.move(+1.05)
    world.tick()
    assert world.round(r.id).state == "CLOSING", "open rounds are still managed while halted"

    world.feeds.trading_obj = make_trading()
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.active() == [], "halt is latched"
    world.store.kv_delete("halt")
    world.tick()
    assert len(world.active()) == 1


def test_intake_pause_blocks_new_rounds(world):
    world.feeds.trading_obj = make_trading(paused_actions=frozenset({"openPosition"}))
    world.tick()
    assert world.active() == []
    assert "market_closed" in world.alert_kinds()


# ---------------------------------------------------------------- restarts
def test_restart_while_hedged_resumes_and_closes(world):
    r = world.open_and_hedge()
    world.new_farmer()
    world.feeds.move(-1.05)
    world.tick()
    assert world.round(r.id).state == "CLOSING"
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"


def test_restart_mid_opening_with_one_leg_filled_unwinds_after_timeout(world):
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    world.new_farmer()
    world.tick(world.cfg.leg_fill_timeout_s)
    r = world.round(r.id)
    assert r.state == "CLOSING" and r.short.intent_id in world.venue.cancels
    assert close_ops(world)[0]["position_ids"] == [r.long.position_id]


def test_restart_while_closing_does_not_duplicate_the_close(world):
    world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.new_farmer()
    world.tick()
    world.tick()
    assert len(world.venue.submitted_of("closePositions")) == 1


def test_account_read_failure_takes_no_action(world):
    world.venue.account_down = True
    for _ in range(3):
        world.tick()
    assert world.venue.submitted == []
    assert "account_unavailable" in world.alert_kinds()


# ---------------------------------------------------------------- shutdown
def test_shutdown_with_one_leg_filled_unwinds_before_exit(world):
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    world.farmer.shutting_down = True
    world.tick()
    assert not world.farmer.safe_to_exit()
    r = world.round(r.id)
    assert r.close_reason == "shutdown" and r.short.intent_id in world.venue.cancels
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert not world.farmer.safe_to_exit(), "the cancelled leg is not confirmed gone yet"
    world.tick(130)
    assert world.round(r.id).state == "ABORTED"
    assert world.farmer.safe_to_exit()
    assert world.active() == [], "no new rounds while shutting down"


def test_shutdown_leaves_hedged_pairs_unless_flatten(make_world):
    world = make_world()
    world.open_and_hedge()
    world.farmer.shutting_down = True
    assert world.farmer.safe_to_exit()

    world = make_world(FLATTEN_HEDGED_ON_SHUTDOWN="true", DATA_DIR=str(world.tmp_path / "flatten"))
    r = world.open_and_hedge()
    world.farmer.shutting_down = True
    assert not world.farmer.safe_to_exit()
    world.tick()
    assert world.round(r.id).close_reason == "shutdown_flatten"


# ------------------------------------------------------- scenario stop gates
def test_lp_below_floor_blocks(world):
    world.feeds.summary_obj = make_summary(tracked_lp=400_000)
    world.tick()
    assert world.active() == [] and "lp_floor" in world.alert_kinds()


def test_lp_trending_below_floor_blocks(world):
    world.feeds.set_queue(1, 1)
    lp = 1_300_000.0
    for _ in range(6):
        world.feeds.summary_obj = make_summary(tracked_lp=lp)
        world.tick(300)
        lp -= 50_000  # -600k/h: below $0.5M within the 2h horizon
    world.feeds.set_queue(0, 0)
    world.tick(1)
    assert world.active() == []
    assert "LP trending below floor" in json.dumps(world.events("alert"))


def test_mint_rate_stop_only_when_no_surplus(world):
    world.feeds.summary_obj = make_summary(tail=200_000_000)  # rate ~14
    world.tick()
    assert world.active() == [] and "mint_rate_stop" in world.alert_kinds()
    world.feeds.summary_obj = make_summary(tail=200_000_000, excess=250_000)
    world.tick()
    assert len(world.active()) == 1


def run_cycle(world, close_delay_s: float):
    world.feeds.move(0)
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.clock.advance(close_delay_s)
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    return world.round(r.id)


def test_repeated_slow_closes_alert_and_pause(make_world):
    world = make_world(CLOSE_LATENCY_ALERT_S=300, CLOSE_LATENCY_ALERT_COUNT=2)
    assert run_cycle(world, 400).close_latency_s > 300
    assert "slow_closes" not in world.alert_kinds(), "one slow close is not a pattern"
    run_cycle(world, 400)
    assert "slow_closes" in world.alert_kinds()
    assert world.active() == []
    assert "close_latency" in world.farmer.status()["blocked"]["BTC"]


# ------------------------------------------------------------------ tranches
TRANCHES = json.dumps([
    {"name": "today", "budget_usd": 100},
    {"name": "pre_wave", "budget_usd": 500, "available_until": "2026-10-12T00:00:00Z"},
    {"name": "reserve", "budget_usd": 300, "kind": "opportunistic"},
])


def test_rounds_draw_from_first_eligible_tranche(make_world):
    world = make_world(BUDGET_TRANCHES=TRANCHES)
    world.tick()
    assert world.only_round().tranche == "today"


def test_tranche_falls_through_when_budget_is_short(make_world):
    world = make_world(BUDGET_TRANCHES=TRANCHES.replace('"budget_usd": 100', '"budget_usd": 50'))
    world.tick()
    assert world.only_round().tranche == "pre_wave"


def test_tranche_deadline_and_min_mint_rate(make_world):
    world = make_world(BUDGET_TRANCHES=TRANCHES.replace('"budget_usd": 100', '"budget_usd": 50'))
    world.clock.t += 2 * 86400  # past pre_wave's deadline
    world.tick()
    assert world.active() == [] and "tranche_unavailable" in world.alert_kinds()

    world = make_world(BUDGET_TRANCHES=TRANCHES, DATA_DIR=str(world.tmp_path / "rate"))
    world.feeds.summary_obj = make_summary(tail=125_000_000)  # rate ~24.5 < 28 but above the stop of 20
    world.tick()
    assert world.active() == []
    assert world.farmer.status()["blocked"]["BTC"] == ["tranche"]
    assert "mint rate 24.0 < 28.0" in json.dumps(world.events("alert"))


def test_opportunistic_tranche_spends_reserve_in_small_rounds(make_world):
    tranches = json.dumps([{"name": "spent", "budget_usd": 10}, {"name": "reserve", "budget_usd": 300, "kind": "opportunistic"}])
    world = make_world(BUDGET_TRANCHES=tranches, MARGIN_PER_LEG_USD=1000, DAILY_LOSS_CAP_USD=1000)
    world.tick()
    assert world.active() == [], "LP above $2M: reserve is not active"

    world.feeds.summary_obj = make_summary(tracked_lp=1_500_000)
    world.tick()
    r = world.only_round()
    assert r.tranche == "reserve"
    assert r.margin_usd == 250.0, "protocol minimum at 40x"


def test_rounds_shrink_to_fit_the_remaining_tranche(make_world):
    tranches = json.dumps([{"name": "t1", "budget_usd": 500}])
    world = make_world(BUDGET_TRANCHES=tranches, MARGIN_PER_LEG_USD=2750, DAILY_LOSS_CAP_USD=1000,
                       ROUND_COST_SAFETY_FACTOR=1.25, SIZE_FROM_BALANCE="true")
    world.tick()
    first = world.only_round()
    assert first.margin_usd == 2750, "a full-size round fits a fresh $500 tranche at factor 1.25"

    run_cycle(world, 0)
    second = world.only_round()
    used = -world.round(first.id).net_pnl_usd
    per_margin = 1.25 * economics.worst_case_round_loss_usd(1.0, 40, 0.01, 0.000476041580087574, economics.BTC_LAUNCH_PARAMS)
    assert second.margin_usd < 2750
    assert used + second.margin_usd * per_margin <= 500 + 0.01, "never budgets past the tranche"


def test_daily_cap_shrinks_rounds_before_blocking(make_world):
    world = make_world(MARGIN_PER_LEG_USD=1000, DAILY_LOSS_CAP_USD=100)
    world.tick()
    r = world.only_round()
    assert 250 <= r.margin_usd < 1000


def test_flat_region_mint_rate():
    assert economics.mint_rate(69e6, 100, 120e6, tracked_lp_usd=1.5e6, cliff_usd=2e6) == 100
    assert economics.mint_rate(69e6, 100, 120e6, tracked_lp_usd=3e6, cliff_usd=2e6) < 41
