"""The cost model, checked against the formulas observed on real closes."""

from __future__ import annotations

import math

import pytest

from ptfarmer import economics as e
from ptfarmer.value_side import payback_days, slope_per_hour

BTC = e.BTC_LAUNCH_PARAMS
TAIL_NOW = 69_310_000.0  # tail progress at the time of writing
LP_NOW = 3_130_000.0


def test_mint_rate_ratchet_and_flat_region():
    assert e.mint_rate(0) == 100
    assert e.mint_rate(120e6) == pytest.approx(25)
    assert e.mint_rate(TAIL_NOW, tracked_lp_usd=LP_NOW, cliff_usd=2e6) == pytest.approx(40.18, abs=0.01)
    assert e.mint_rate(TAIL_NOW, tracked_lp_usd=1.9e6, cliff_usd=2e6) == 100
    assert e.mint_rate(TAIL_NOW, tracked_lp_usd=-5e5, cliff_usd=2e6) == 100


def test_win_side_impact_and_fee():
    move = 0.01
    eff = move - e.DEADBAND
    gross = eff * e.win_scale(eff, BTC)
    assert e.winning_close_received(move, BTC) == pytest.approx(gross * 0.98)
    assert 0.0085 < gross < 0.009, "a 1% BTC win keeps ~87% after the impact scale"
    assert e.winning_close_received(e.DEADBAND / 2, BTC) == 0


def test_hedged_cycle_cost_per_paper_at_current_state():
    est = e.estimate_cost_per_paper(0.01, BTC, TAIL_NOW, 2.17e6, 0.025, 0.25, tracked_lp_usd=LP_NOW, cliff_usd=2e6)
    assert est.spot.cost_per_paper == pytest.approx(0.00346, abs=0.00005)
    assert est.cost_per_paper > est.spot.cost_per_paper, "decay makes the close-time cost higher"
    assert 0.0034 <= est.cost_per_paper <= 0.0039, "matches the strategy note's all-in range"
    assert est.rate_decay_per_hour == pytest.approx(0.0226, abs=0.001)
    assert est.cycle_hours == pytest.approx(0.01**2 / (0.025**2 / 24) + 0.25)


def test_flat_region_has_no_decay():
    est = e.estimate_cost_per_paper(0.01, BTC, TAIL_NOW, 2.17e6, 0.025, 0.25, tracked_lp_usd=1.5e6, cliff_usd=2e6)
    assert est.spot.mint_rate == est.at_close.mint_rate == 100
    assert est.rate_decay_per_hour == 0


def test_queue_open_removes_the_loss_carve():
    empty = e.hedged_cycle(0.01, BTC, 40, queue_empty=True)
    full = e.hedged_cycle(0.01, BTC, 40, queue_empty=False)
    assert full.paper_per_notional == pytest.approx(empty.paper_per_notional / 0.98)


def test_worst_case_covers_liquidation():
    margin, lev = 250, 40
    liq = e.liquidated_round_cost_usd(margin, lev, 0.000476, BTC)
    normal = e.expected_round_cost_usd(margin * lev, 0.01, BTC)
    assert e.worst_case_round_loss_usd(margin, lev, 0.01, 0.000476, BTC) == max(liq, normal)
    assert 0 < normal < liq


def test_payback_days():
    assert payback_days(0.0035, 0.0001, 0.0) == pytest.approx(35)
    assert payback_days(0.0035, 0.0001, -0.5) is None, "a fast-decaying yield never pays back"
    assert payback_days(0.0035, None, 0.0) is None
    growing = payback_days(0.0035, 0.0001, 0.01)
    assert growing < 35 and math.isfinite(growing)


def test_slope_per_hour():
    pts = [(i * 600.0, 1e6 - i * 50_000.0) for i in range(5)]
    assert slope_per_hour(pts) == pytest.approx(-300_000)
    assert slope_per_hour(pts[:2]) is None, "span too short"
