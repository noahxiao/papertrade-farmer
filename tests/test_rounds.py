"""Round lifecycle against the scripted relayer: the never-naked-side guarantees."""

from __future__ import annotations

from ptfarmer.relayer import SubmitStatus


def close_ops(world):
    return world.venue.ops_of("closePositions")


def test_both_legs_fill_then_one_close_order_at_take_profit(world):
    r = world.open_and_hedge()
    assert world.naked_exposure() == 0
    assert r.entry_ref == world.feeds.mids["BTC"]

    world.feeds.move(+0.5)
    world.tick()
    assert world.round(r.id).state == "HEDGED", "no close below the take-profit move"

    world.feeds.move(+1.05)
    world.tick()
    r = world.round(r.id)
    assert (r.state, r.close_reason) == ("CLOSING", "take_profit")
    [close] = close_ops(world)
    assert sorted(close["position_ids"]) == sorted([r.long.position_id, r.short.position_id]), "both legs in ONE close"

    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    r = world.round(r.id)
    assert r.state == "DONE"
    assert r.paper_minted > 0 and r.net_pnl_usd < 0
    assert r.cost_per_paper == -r.net_pnl_usd / r.paper_minted
    assert r.close_latency_s == 10
    assert world.events("round_closed")[0]["outcome"] == "DONE"
    assert (world.cfg.data_dir / "rounds.jsonl").read_text().count('"DONE"') >= 1


def test_next_round_starts_immediately_after_close(world):
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"
    [nxt] = world.active()
    assert nxt.id != r.id and nxt.state == "OPENING"


def test_cooldown_delays_the_next_round(make_world):
    world = make_world(ROUND_COOLDOWN_S=60)
    world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.active() == []
    world.tick(61)
    assert len(world.active()) == 1


def test_one_leg_fills_other_rejected_unwinds_the_filled_leg(world):
    world.venue.script(SubmitStatus.ACCEPTED, (SubmitStatus.REJECTED, "invalid_intent"))
    world.tick()
    r = world.only_round()
    assert (r.long.status, r.short.status) == ("pending", "dead")

    world.venue.fill(r.long.intent_id)
    world.tick()
    r = world.round(r.id)
    assert (r.state, r.close_reason) == ("CLOSING", "other_leg_dead")
    assert "one_leg_unwind" in world.alert_kinds()
    [close] = close_ops(world)
    assert close["position_ids"] == [r.long.position_id]

    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "ABORTED"
    assert world.venue.positions == {}


def test_first_leg_rejected_means_second_leg_is_never_sent(world):
    world.venue.script((SubmitStatus.REJECTED, "below_minimum_trade_size"))
    world.tick()
    assert len(world.venue.submitted_of("openPosition")) == 1
    r = world.only_round()
    assert r.short.submit_status == "not_submitted"
    world.tick()
    assert world.round(r.id).state == "ABORTED"
    assert world.venue.positions == {}


def test_unfilled_partner_is_cancelled_when_the_hedge_fails(world):
    world.venue.script(SubmitStatus.ACCEPTED, (SubmitStatus.REJECTED, "intent_conflict"))
    world.tick()
    r = world.only_round()
    world.tick()
    assert r.long.intent_id in world.venue.cancels, "a pending leg whose partner died is cancelled"


def test_leg_fill_timeout_cancels_pending_leg_and_closes_filled_leg(world):
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    world.tick()
    assert world.round(r.id).state == "OPENING"

    world.tick(world.cfg.leg_fill_timeout_s)
    r = world.round(r.id)
    assert (r.state, r.close_reason) == ("CLOSING", "leg_fill_timeout")
    assert r.short.intent_id in world.venue.cancels
    assert close_ops(world)[0]["position_ids"] == [r.long.position_id]


def test_acknowledged_cancel_resolves_the_leg_without_waiting_for_expiry(world):
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    world.tick(world.cfg.leg_fill_timeout_s)
    world.venue.land_close(close_ops(world)[0]["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "CLOSING"
    world.tick(130)
    r = world.round(r.id)
    assert r.state == "ABORTED" and r.short.status == "dead"
    assert world.events("leg_dead")[0]["cancelled"] is True


def test_late_fill_after_timeout_is_closed_too(world):
    world.venue.lose_cancels = True
    world.tick()
    r = world.only_round()
    world.venue.fill(r.long.intent_id)
    world.tick(world.cfg.leg_fill_timeout_s + 10)
    assert world.round(r.id).state == "CLOSING"

    late = world.venue.fill(r.short.intent_id)  # the cancel lost the race
    world.tick()
    assert [e["side"] for e in world.events("late_fill")] == ["short"]
    r = world.round(r.id)
    covered = sorted(pid for op in close_ops(world) for pid in op["position_ids"])
    assert covered == sorted([r.long.position_id, late.position_id])

    for op in close_ops(world):
        world.venue.land_close(op["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "ABORTED"
    assert world.venue.positions == {}


def test_fill_after_round_is_finished_is_swept(world):
    world.venue.lose_cancels = True
    world.tick()
    r = world.only_round()
    world.feeds.set_queue(1, 1)  # keep new rounds out of the way
    world.tick(world.cfg.open_abandon_timeout_s)
    assert set(world.venue.cancels) >= {r.long.intent_id, r.short.intent_id}
    world.tick(3600 + world.cfg.intent_grace_s)
    assert world.round(r.id).state == "ABORTED"

    pos = world.venue.fill(r.long.intent_id)
    world.feeds.set_queue(0, 0)
    world.tick()
    assert "orphan_position" in world.alert_kinds()
    assert world.active() == [], "no new round while an orphan is being swept"
    [close] = close_ops(world)
    assert close["position_ids"] == [pos.position_id]

    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    assert world.events("sweep_closed")[0]["position_id"] == pos.position_id
    assert world.venue.positions == {}


def test_unknown_position_is_closed_or_halts_per_policy(make_world):
    world = make_world()
    world.feeds.set_queue(1, 1)
    pos = world.venue.external_position()
    world.tick()
    assert close_ops(world)[0]["position_ids"] == [pos.position_id]

    world = make_world(UNKNOWN_POSITION_POLICY="halt", DATA_DIR=str(world.tmp_path / "halt"))
    world.venue.external_position()
    world.tick()
    assert world.farmer.halted()["reason"] == "unknown_position"
    assert close_ops(world) == []


def test_losing_leg_liquidated_winner_is_closed_and_farming_continues(world):
    r = world.open_and_hedge()
    world.feeds.move(+2.6)  # gap through take-profit and the short's bust price
    world.venue.liquidate(r.short.position_id)
    world.tick()
    r = world.round(r.id)
    assert (r.state, r.close_reason) == ("CLOSING", "loser_liquidated")
    assert world.events("leg_liquidated")[0]["side"] == "short"
    [close] = close_ops(world)
    assert close["position_ids"] == [r.long.position_id]

    world.venue.land_close(close["intent_id"], world.feeds.mids["BTC"])
    world.tick()
    r = world.round(r.id)
    assert r.state == "DONE" and r.liquidated
    assert not world.farmer.halted()
    assert len(world.active()) == 1, "next round starts"


def test_close_is_resigned_before_expiry(world):
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    [first] = close_ops(world)
    world.clock.t = first["deadline"] - world.cfg.close_resign_before_expiry_s + 1
    world.tick(0)
    closes = world.venue.submitted_of("closePositions")
    assert len(closes) == 2 and closes[1].position_ids == closes[0].position_ids
    assert [c.status for c in world.round(r.id).closes] == ["superseded", "accepted"]
    assert world.events("close_resigned")

    world.venue.land_close(closes[1].intent_id, world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"


def test_rejected_resign_waits_for_expiry_then_resubmits(world):
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    [first] = close_ops(world)
    world.venue.script((SubmitStatus.REJECTED, "admission_rejected"))
    world.clock.t = first["deadline"] - world.cfg.close_resign_before_expiry_s + 1
    world.tick(0)
    assert world.round(r.id).closes[0].code == "resign_blocked"
    world.tick(60)
    assert len(world.venue.submitted_of("closePositions")) == 2, "no resubmission while the original is live"

    world.clock.t = first["deadline"] + 1
    world.tick(0)
    third = world.venue.submitted_of("closePositions")[-1]
    assert len(world.venue.submitted_of("closePositions")) == 3
    world.venue.land_close(third.intent_id, world.feeds.mids["BTC"])
    world.tick()
    assert world.round(r.id).state == "DONE"


def test_close_missing_from_queue_is_resubmitted(world):
    r = world.open_and_hedge()
    world.feeds.move(+1.05)
    world.tick()
    world.venue.expire(close_ops(world)[0]["intent_id"])  # relayer dropped it
    world.tick(130)
    assert len(world.venue.submitted_of("closePositions")) == 2
    assert world.round(r.id).state == "CLOSING"
