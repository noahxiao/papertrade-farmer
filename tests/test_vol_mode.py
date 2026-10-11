"""Volatility-matched close distance, pace tightening, close-quality derating and LP-floor tranches."""

from __future__ import annotations

import json

import pytest

from fakes import BTC_MID, T0, make_summary
from ptfarmer.config import DEFAULT_BUST_BUFFER, ConfigError, load_config
from ptfarmer.pace import Checkpoint, PaceError, parse_schedule, pace_status
from ptfarmer.tranches import Tranche, TrancheUsage, effective_budget, tranche_room
from ptfarmer.volatility import CLOSE_QUALITY_KEY, VolReading, choose_shape, completed_closes, realised_hourly_vol

VOL = {"CLOSE_MODE": "vol_matched"}


def cfg(**env):
    return load_config(env={"MAX_COST_PER_PAPER": "off", **VOL, **{k: str(v) for k, v in env.items()}})


def shape_at(sigma, c=None, **kw):
    return choose_shape(c or cfg(MAX_LEVERAGE=170), "BTC", VolReading(sigma, "measured"), DEFAULT_BUST_BUFFER, 1000, **kw)


def close_ops(world):
    return world.venue.ops_of("closePositions")


def run_cycle(world, move_pct: float, close_delay_s: float = 10, land_pct: float | None = None):
    """One hedged round: fill at mid, move to trigger the close, land it after `close_delay_s` at `land_pct`."""
    world.feeds.move(0)
    r = world.open_and_hedge()
    world.feeds.move(move_pct)
    world.tick()
    assert world.round(r.id).state == "CLOSING"
    world.clock.advance(close_delay_s)
    world.venue.land_close(close_ops(world)[0]["intent_id"], BTC_MID * (1 + (land_pct or move_pct) / 100))
    world.tick()
    return world.round(r.id)


# ------------------------------------------------------------ pure shape math
def test_realised_vol_matches_constructed_returns():
    closes = [100.0, 101.0] * 13
    sigma = realised_hourly_vol(closes, 900)
    assert sigma == pytest.approx(abs(__import__("math").log(1.01)) * 2, rel=1e-9)
    assert realised_hourly_vol(closes[:4], 900) is None, "fewer than 4 returns is not a reading"


def test_completed_closes_drops_the_forming_candle_and_keeps_the_window():
    now_ms = 10 * 900_000 + 5
    candles = [{"t": i * 900_000, "T": i * 900_000 + 899_999, "c": float(i + 1)} for i in range(11)]
    closes = completed_closes(candles, now_ms, window_h=1.0, interval_s=900)
    assert closes == [6.0, 7.0, 8.0, 9.0, 10.0], "4 returns of completed candles; the forming one (11) is excluded"


@pytest.mark.parametrize("sigma,d,lev", [
    (0.0009, 0.003, 166),   # weekend: 2 x 0.09% = 0.18% -> floor 0.30%; 1/(0.30%+0.30%) binds
    (0.0020, 0.004, 140),   # TP <= 60% of liquidation binds (formula alone would give 142)
    (0.0025, 0.005, 113),
    (0.0100, 0.0075, 77),   # clamped to the 0.75% ceiling
])
def test_close_distance_and_leverage_follow_vol_inside_the_safety_rules(sigma, d, lev):
    s = shape_at(sigma)
    assert (s.tp_move, s.leverage) == (pytest.approx(d), lev)
    assert s.tp_fraction_of_liq <= 0.60 + 1e-9
    assert s.gap_to_liq >= 0.002
    assert 0 < s.emergency_distance < s.gap_to_liq, "the emergency close never fires before the take-profit"


def test_default_max_leverage_caps_at_100():
    s = shape_at(0.0009, cfg())
    assert (s.tp_move, s.leverage) == (pytest.approx(0.003), 100)


def test_unavailable_vol_uses_the_widest_close():
    s = choose_shape(cfg(), "BTC", VolReading(None, "unavailable"), DEFAULT_BUST_BUFFER, 1000)
    assert s.tp_move == pytest.approx(0.0075) and s.leverage == 77 and "unavailable" in s.note


def test_behind_pace_tightens_to_the_floor():
    s = shape_at(0.003, behind_pace=True)
    assert s.tp_move == pytest.approx(0.003) and s.behind_pace and s.leverage == 166


def test_derate_steps_lower_leverage():
    levs = [shape_at(0.0009, derate_steps=n).leverage for n in range(3)]
    assert levs == [166, 142, 125]


# ---------------------------------------------------------------- config
def test_max_leverage_may_be_raised_explicitly_but_not_past_the_ceiling():
    assert cfg(MAX_LEVERAGE=170).max_leverage == 170
    with pytest.raises(ConfigError, match="MAX_LEVERAGE"):
        cfg(MAX_LEVERAGE=201)


def test_fixed_mode_leverage_respects_max_leverage_and_liquidation():
    with pytest.raises(ConfigError, match="MAX_LEVERAGE=100"):
        load_config(env={"LEVERAGE": "150", "TAKE_PROFIT_MOVE_PCT": "0.3"})
    c = load_config(env={"LEVERAGE": "150", "MAX_LEVERAGE": "170", "TAKE_PROFIT_MOVE_PCT": "0.3"})
    assert c.leverage == 150
    with pytest.raises(ConfigError, match="60%|liquidation"):
        load_config(env={"LEVERAGE": "150", "MAX_LEVERAGE": "170", "TAKE_PROFIT_MOVE_PCT": "0.5"})


@pytest.mark.parametrize("env,match", [
    ({"CLOSE_MIN_PCT": "0.8", "CLOSE_MAX_PCT": "0.5"}, "CLOSE_MIN_PCT"),
    ({"VOL_LEVERAGE_BUFFER_PCT": "0.1"}, "VOL_LEVERAGE_BUFFER_PCT"),
    ({"VOL_CANDLE_INTERVAL": "3m"}, "VOL_CANDLE_INTERVAL"),
    ({"PACE_SCHEDULE": "2026-10-12T01:00:00Z=800,2026-10-12T19:00:00Z=450"}, "decrease"),
    ({"PACE_SCHEDULE": "tomorrow=450"}, "PACE_SCHEDULE"),
])
def test_bad_vol_settings_are_rejected(env, match):
    with pytest.raises(ConfigError, match=match):
        cfg(**env)


# ------------------------------------------------------------------- pace
def test_pace_is_behind_only_once_a_checkpoint_has_passed():
    sched = parse_schedule("2026-10-12T19:00:00Z=800,2026-10-12T01:00:00Z=450")
    assert [c.spent_usd for c in sched] == [450, 800]
    before, mon_noon = sched[0].at - 1, sched[0].at
    assert not pace_status(sched, 0, before).behind
    assert pace_status(sched, 449, mon_noon).behind
    assert not pace_status(sched, 450, mon_noon).behind
    assert pace_status(sched, 700, sched[1].at).behind
    assert "BEHIND" in pace_status(sched, 100, mon_noon).describe()
    with pytest.raises(PaceError):
        parse_schedule("2026-10-12T01:00:00Z")


# -------------------------------------------------------- remainder budget
def test_remainder_tranche_farms_only_what_the_plan_has_not_spent():
    tonight = Tranche("tonight", 1000.0)
    band = Tranche("flat_band", 0.0, "opportunistic", remainder=True)
    ts = (tonight, band)
    usage = {"tonight": TrancheUsage(-600.0)}
    assert tranche_room(band, ts, usage) == pytest.approx(400)
    usage["flat_band"] = TrancheUsage(-300.0)
    assert tranche_room(band, ts, usage) == pytest.approx(100)
    assert tranche_room(tonight, ts, usage) == pytest.approx(100), "remainder spending also reduces the standard tranche"
    assert effective_budget(band, ts, usage) == pytest.approx(400)


# ---------------------------------------------------------------- engine
def test_vol_mode_round_uses_the_measured_shape(make_world):
    world = make_world(**VOL, MARGIN_PER_LEG_USD=1400, DAILY_LOSS_CAP_USD=1000)
    world.venue.balance = 4000
    world.tick()
    r = world.only_round()
    assert (r.close_mode, r.tp_move, r.leverage, r.margin_usd) == ("vol_matched", pytest.approx(0.003), 100, 1400)
    assert r.sigma_hourly == pytest.approx(0.0009, rel=1e-6)
    assert r.liq_move == pytest.approx(0.01 - DEFAULT_BUST_BUFFER, rel=1e-4)
    world.venue.fill(r.long.intent_id)
    world.venue.fill(r.short.intent_id)
    world.tick()
    world.feeds.move(+0.25)
    world.tick()
    assert world.round(r.id).state == "HEDGED", "0.25% is inside a 0.30% close"
    world.feeds.move(+0.31)
    world.tick()
    assert world.round(r.id).state == "CLOSING" and len(close_ops(world)) == 1
    assert sorted(close_ops(world)[0]["position_ids"]) == sorted(world.round(r.id).filled_position_ids()), \
        "both legs close in one Close order"


def test_vol_feed_outage_falls_back_to_widest_close_and_alerts(make_world):
    world = make_world(**VOL)
    world.feeds.candles_down = True
    world.tick()
    r = world.only_round()
    assert (r.tp_move, r.leverage) == (pytest.approx(0.0075), 77)
    assert "volatility_unavailable" in world.alert_kinds()


def test_vol_reading_goes_stale_then_unavailable(make_world):
    world = make_world(**VOL, ROUND_COOLDOWN_S=10**9)
    world.tick()
    world.feeds.candles_down = True
    world.tick(400)
    assert world.farmer.last_ctx.shapes["BTC"].sigma_source == "stale"
    world.tick(3600)
    assert world.farmer.last_ctx.shapes["BTC"].sigma_source == "unavailable"


def test_behind_pace_tightens_the_next_round(make_world):
    world = make_world(**VOL, PACE_START="2026-10-10T00:00:00Z", PACE_SCHEDULE="2026-10-11T00:00:00Z=450")
    world.feeds.sigma_hourly = 0.003  # 2 x 0.3% = 0.6% before tightening
    world.tick()
    r = world.only_round()
    assert r.behind_pace and r.tp_move == pytest.approx(0.003)
    assert "behind_pace" in world.alert_kinds()


def test_repeated_slow_closes_lower_leverage_then_pause(make_world):
    world = make_world(**VOL, MAX_LEVERAGE=170, CLOSE_LATENCY_ALERT_S=180, CLOSE_LATENCY_ALERT_COUNT=2)
    levs = []
    for _ in range(6):
        levs.append(run_cycle(world, +0.32, close_delay_s=200).leverage)
    assert levs == [166, 166, 142, 142, 125, 125]
    assert world.alert_kinds().count("leverage_derated") == 2
    assert "slow_closes" in world.alert_kinds()
    assert world.active() == [] and "close_latency" in world.farmer.status()["blocked"]["BTC"]


def test_overshooting_close_counts_as_bad_and_clean_closes_restore_leverage(make_world):
    world = make_world(**VOL, MAX_LEVERAGE=170, CLOSE_LATENCY_ALERT_COUNT=2)
    for _ in range(2):
        r = run_cycle(world, +0.32, land_pct=+0.50)  # fast, but 78% of the way from TP to liquidation
        assert r.overshoot_fraction > 0.5
    assert world.store.kv_get(CLOSE_QUALITY_KEY)["derate_steps"] == 1
    for _ in range(5):
        r = run_cycle(world, +0.32)
        assert r.leverage == 142
    assert world.store.kv_get(CLOSE_QUALITY_KEY)["derate_steps"] == 0
    assert "leverage_restored" in world.alert_kinds()


def test_fixed_mode_slow_closes_still_pause_without_derating(make_world):
    world = make_world(CLOSE_LATENCY_ALERT_S=300, CLOSE_LATENCY_ALERT_COUNT=2)
    run_cycle(world, +1.05, close_delay_s=400)
    run_cycle(world, +1.05, close_delay_s=400)
    assert "slow_closes" in world.alert_kinds() and "leverage_derated" not in world.alert_kinds()


# --------------------------------------------- LP floors and the flat band
PLAN = json.dumps([
    {"name": "tonight", "budget_usd": 1000, "available_until": "2026-10-13T02:00:00Z"},
    {"name": "flat_band", "budget_usd": "remainder", "kind": "opportunistic"},
])
FLOORS = dict(VOL, BUDGET_TRANCHES=PLAN, LP_FLOOR_USD=1_000_000, OPPORTUNISTIC_LP_MIN_USD=500_000,
              OPPORTUNISTIC_MARGIN_PER_LEG_USD=200, MARGIN_PER_LEG_USD=1400, DAILY_LOSS_CAP_USD=1000)


@pytest.mark.parametrize("lp,tranche,margin", [
    (3_100_000, "tonight", 1400),
    (1_500_000, "tonight", 1400),   # flat-100 band with LP above $1M: standard tranche
    (800_000, "flat_band", 200),    # below $1M: standard stops, the remainder farms in small rounds
])
def test_lp_bands_pick_the_tranche_and_size(make_world, lp, tranche, margin):
    world = make_world(**FLOORS)
    world.venue.balance = 4000
    world.feeds.summary_obj = make_summary(tracked_lp=lp)
    world.tick()
    r = world.only_round()
    assert (r.tranche, r.margin_usd) == (tranche, margin)


def test_lp_below_half_a_million_stops(make_world):
    world = make_world(**FLOORS)
    world.feeds.summary_obj = make_summary(tracked_lp=400_000)
    world.tick()
    assert world.active() == []
    assert "lp_floor" in world.farmer.status()["blocked"]["BTC"]


def test_open_queue_stops_even_in_the_flat_band(make_world):
    world = make_world(**FLOORS)
    world.feeds.summary_obj = make_summary(tracked_lp=800_000)
    world.feeds.set_queue(5_000.0, 3)
    world.tick()
    assert world.active() == [] and "payout_queue" in world.farmer.status()["blocked"]["BTC"]


def test_mint_rate_below_28_stops_and_keeps_the_budget(make_world):
    world = make_world(**FLOORS)
    world.feeds.summary_obj = make_summary(tail=110_000_000)  # ~27.3 PAPER/$
    world.tick()
    assert world.active() == []
    assert "tranche" in world.farmer.status()["blocked"]["BTC"]
    assert world.farmer.tranche_usage([]) == {}, "nothing was committed from the plan"


def test_monitor_and_summary_show_the_shape_and_pace(make_world):
    from ptfarmer.gates import plan_round
    from ptfarmer.reporting import daily_summary_text, monitor_json, monitor_report, summarize_day
    world = make_world(**VOL, PACE_START="2026-10-11T00:00:00Z", PACE_SCHEDULE="2026-10-12T01:00:00Z=450")
    world.tick()
    ctx, account = world.farmer.last_ctx, world.venue.account()
    plan = plan_round(world.cfg, ctx, "BTC", account, world.farmer.breaker, world.farmer.loss_budget(ctx.now, []),
                      None, {})
    text = monitor_report(world.cfg, ctx, world.baseline, account, plan.gates, [], {}, "budget")
    assert "BTC 100x, close at 0.30% (vol_matched)" in text and "sigma 0.090%/h (measured)" in text
    assert "Pace: spent $0" in text
    data = json.loads(monitor_json(world.cfg, ctx, account, plan.gates, []))
    assert data["close_shape"]["BTC"]["leverage"] == 100 and data["pace"]["behind"] is False
    summary = daily_summary_text(world.cfg, summarize_day(world.store, T0), {}, ctx, account, world.baseline, ctx.now)
    assert "Next round BTC: 100x" in summary and "Pace:" in summary
