"""The farming engine: one `tick()` reconciles the account and advances every round.

Invariants (each tick):
  1. The account snapshot (positions + pending operations) is the source of
     truth. No action is taken without a fresh snapshot.
  2. Every open position is claimed by an active round or swept (closed).
  3. A round never leaves CLOSING while any of its positions is open or any
     of its open intents could still land.
  4. New rounds start only when every entry gate passes.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Callable

from . import budget
from .alerts import Notifier
from .config import Config
from .context import MarketContext, MarketView
from .feeds import FeedError, PublicFeeds
from .budget import utc_day_start
from .gates import LossBudget, blocking, plan_round
from .guards import RelayerBreaker, check_contracts, check_frontend, check_protocol_params
from .models import AccountSnapshot, ClosedTrade, FeedFormatError
from .protocol import INSTRUMENTS, MAX_CLOSE_IDS
from .relayer import SubmitResult, SubmitStatus
from .rounds import CloseRecord, Leg, LegStatus, Round, RoundState, close_overshoot
from .staking import NoStaking, StakingModule
from .store import Store
from .tranches import TrancheUsage
from .venue import PreparedIntent, Venue
from .volatility import CLOSE_QUALITY_KEY, VOL_MATCHED, CloseShape

log = logging.getLogger(__name__)

CLOSE_MISSING_S = 120
CANCEL_CONFIRM_S = 120
MINT_CHECK_TOLERANCE = 0.10
STAKE_MIN_INTERVAL_S = 300
SWEEP_KEY = "sweep"
HALT_KEY = "halt"
PACE_KEY = "pace"

GATE_ALERTS = {
    "cost_per_paper": ("cost_gate", "warning"),
    "daily_loss": ("daily_loss_cap", "error"),
    "payout_queue": ("iou_queue_open", "warning"),
    "relayer": ("relayer_unavailable", "warning"),
    "session_key": ("session_key", "error"),
    "market_open": ("market_closed", "warning"),
    "size": ("insufficient_size", "warning"),
    "lp_floor": ("lp_floor", "error"),
    "mint_rate_stop": ("mint_rate_stop", "warning"),
    "close_latency": ("close_latency", "warning"),
    "tranche": ("tranche_unavailable", "info"),
}
CLOSE_QUALITY_DEFAULT = {"slow_streak": 0, "good_streak": 0, "derate_steps": 0, "paused_until": 0.0}
DERATE_RECOVERY_CLOSES = 5

FEED_ERRORS = (FeedError, FeedFormatError)


class Farmer:
    def __init__(
        self,
        cfg: Config,
        venue: Venue,
        view: MarketView,
        store: Store,
        notifier: Notifier,
        baseline: dict,
        staker: StakingModule | None = None,
        rule_feeds: PublicFeeds | None = None,
        session_address: str | None = None,
        clock: Callable[[], float] = time.time,
        on_build_id: Callable[[str], None] | None = None,
    ):
        self.cfg = cfg
        self.venue = venue
        self.view = view
        self.store = store
        self.notifier = notifier
        self.baseline = baseline
        self.staker = staker or NoStaking()
        self.rule_feeds = rule_feeds
        self.session_address = session_address
        self.clock = clock
        self.on_build_id = on_build_id
        self.breaker = RelayerBreaker(cfg.relayer_max_consecutive_failures, cfg.relayer_down_halt_s, clock)
        self.shutting_down = False
        self.last_rule_check = 0.0
        self.blocked: dict[str, set[str]] = {}
        self.account_failures = 0
        self.last_account: AccountSnapshot | None = None
        self.last_ctx: MarketContext | None = None
        self._trades_cache: tuple[float, list[ClosedTrade]] | None = None
        self._last_stake_at = 0.0
        self.last_tick_ok = False

    # ------------------------------------------------------------------ halt
    def halted(self) -> dict | None:
        return self.store.kv_get(HALT_KEY)

    def halt(self, reason: str, detail: str) -> None:
        if self.halted():
            return
        self.store.kv_set(HALT_KEY, {"reason": reason, "detail": detail, "ts": self.clock()})
        self.notifier.alert("halted", f"{reason}: {detail}. No new rounds until `ptfarmer resume`.", level="critical")

    # ------------------------------------------------------------------ tick
    def tick(self) -> None:
        now = self.clock()
        self._trades_cache = None
        ctx = None
        try:
            ctx = self.view.refresh()
            self.breaker.record_health(ctx.relayer_ok)
            self.last_ctx = ctx
        except FEED_ERRORS as exc:
            self.notifier.alert("feeds_unavailable", f"public feeds unavailable: {exc}", cooldown_s=900)

        try:
            account = self.venue.account()
            self.account_failures = 0
            self.last_account = account
        except FEED_ERRORS as exc:
            self.account_failures += 1
            self.last_tick_ok = False
            if self.account_failures >= 3:
                self.notifier.alert("account_unavailable", f"cannot read account state ({self.account_failures}x): {exc}", level="error", cooldown_s=900)
            return

        if ctx is not None:
            self._check_rules(ctx, now)

        rounds = self.active_rounds()
        claimed: set[int] = set()
        for r in rounds:
            self._step_round(r, account, ctx, now)
            claimed.update(leg.position_id for leg in r.legs if leg.position_id is not None)
        self._sweep_orphans(account, claimed, now)

        if self.breaker.should_halt:
            self.notifier.alert("relayer_down", f"relayer degraded for {self.breaker.down_for_s / 60:.0f} min; new rounds stopped",
                                level="error", cooldown_s=3600)

        still_active = [r for r in rounds if r.active]
        if not still_active:
            self._maybe_stake(account, ctx, now)
        if ctx is not None and len(still_active) < len(rounds):
            ctx = self.last_ctx = self.view.reshape(ctx)  # settled rounds move pace and close quality
        if ctx is not None and not self.shutting_down:
            self._maybe_start_rounds(ctx, account, now, still_active)
        self.last_tick_ok = True

    # ------------------------------------------------------------- rule checks
    def _check_rules(self, ctx: MarketContext, now: float) -> None:
        findings = check_protocol_params(self.baseline, ctx.summary, ctx.trading, self.cfg.markets, now)
        if self.rule_feeds is not None and now - self.last_rule_check >= self.cfg.rule_check_interval_s:
            self.last_rule_check = now
            findings += check_contracts(self.baseline, self.rule_feeds)
            fe_findings, build_id = check_frontend(self.baseline, self.rule_feeds)
            findings += fe_findings
            if build_id and self.on_build_id:
                self.on_build_id(build_id)
        halts = [f for f in findings if f.severity == "halt"]
        for f in findings:
            if f.severity == "warn":
                self.notifier.alert(f"rule_{f.check}", f.message, cooldown_s=6 * 3600)
        if halts:
            self.halt("rule_change", "; ".join(f.message for f in halts))

    # ------------------------------------------------------------ persistence
    def active_rounds(self) -> list[Round]:
        return budget.active_rounds(self.store)

    def _save(self, r: Round, closed: bool = False) -> None:
        self.store.update_round(r.id, r.state, r.to_dict(), closed=closed)

    def _event(self, kind: str, r: Round | None = None, level: str = "info", **data) -> None:
        self.store.log_event(kind, level=level, round_id=r.id if r else None, **data)
        getattr(log, "warning" if level in ("warning", "error") else "info")("%s round=%s %s", kind, r.id if r else "-", data)

    # ------------------------------------------------------------- submission
    def _submit(self, prepared: PreparedIntent, round_id: int | None, purpose: str, record: bool = True) -> SubmitResult:
        if record:
            self.store.record_intent(prepared.intent_id, round_id, purpose, prepared.action, prepared.nonce, prepared.deadline,
                                     prepared.signed, prepared.position_ids)
        result = self.venue.submit(prepared)
        if result.intent_id != prepared.intent_id:
            self.store.rename_intent(prepared.intent_id, result.intent_id)
        self.store.set_intent_status(result.intent_id, result.status.value, result.code)
        if result.transport_failure:
            self.breaker.record_failure(result.retry_after_s)
        else:
            self.breaker.record_success()
        self.store.log_event("intent_submitted", round_id=round_id, purpose=purpose, intent_id=result.intent_id,
                             status=result.status.value, code=result.code, reason=result.reason)
        return result

    def _cancel(self, r: Round, leg: Leg) -> None:
        if leg.cancel_requested or not leg.intent_id or leg.status != LegStatus.PENDING.value:
            return
        result = self.venue.cancel(leg.intent_id)
        if not result.transport_failure:
            leg.cancel_requested = True
        if result.status is SubmitStatus.ACCEPTED:
            leg.cancel_acked_at = self.clock()
        self._event("cancel_requested", r, side=leg.side, intent_id=leg.intent_id, status=result.status.value, code=result.code)

    def _closed_trades(self) -> list[ClosedTrade] | None:
        if self._trades_cache is None:
            try:
                self._trades_cache = (self.clock(), self.venue.closed_trades())
            except FEED_ERRORS as exc:
                log.warning("trade history unavailable: %s", exc)
                return None
        return self._trades_cache[1]

    def _trade(self, position_id: int) -> ClosedTrade | None:
        for t in self._closed_trades() or []:
            if t.position_id == position_id:
                return t
        return None

    # ------------------------------------------------------------ round steps
    def _step_round(self, r: Round, account: AccountSnapshot, ctx: MarketContext | None, now: float) -> None:
        self._refresh_legs(r, account, now)
        if r.state == RoundState.OPENING.value:
            self._step_opening(r, now)
        elif r.state == RoundState.HEDGED.value:
            self._step_hedged(r, ctx, now)
        if r.state == RoundState.CLOSING.value:
            self._step_closing(r, account, ctx, now)
        if r.active:
            self._save(r)

    def _refresh_legs(self, r: Round, account: AccountSnapshot, now: float) -> None:
        for leg in r.legs:
            if leg.status == LegStatus.PENDING.value:
                if not leg.intent_id:
                    leg.status = LegStatus.DEAD.value
                    continue
                pos = account.position_by_intent(leg.intent_id)
                if pos is not None:
                    leg.status = LegStatus.FILLED.value
                    leg.position_id, leg.entry_price, leg.margin_usd = pos.position_id, pos.entry_price, pos.margin_usd
                    leg.bust_price, leg.filled_at = pos.bust_price, now
                    self.store.set_intent_status(leg.intent_id, "filled")
                    late = r.state == RoundState.CLOSING.value or leg.cancel_requested
                    self._event("late_fill" if late else "leg_filled", r, level="warning" if late else "info",
                                side=leg.side, position_id=pos.position_id, entry=pos.entry_price, margin=pos.margin_usd)
                elif leg.submit_status == SubmitStatus.REJECTED.value:
                    leg.status = LegStatus.DEAD.value
                elif account.operation(leg.intent_id) is None and self._intent_gone(leg, now):
                    leg.status = LegStatus.DEAD.value
                    self.store.set_intent_status(leg.intent_id, "dead")
                    self._event("leg_dead", r, side=leg.side, intent_id=leg.intent_id,
                                cancelled=leg.cancel_acked_at is not None)
            elif leg.status == LegStatus.FILLED.value and account.position(leg.position_id) is None:
                self._leg_vanished(r, leg)

    def _intent_gone(self, leg: Leg, now: float) -> bool:
        """A pending open that is not in the account's queue can no longer fill: expired, or cancelled and confirmed.

        An acknowledged cancel that still loses the race shows up later as a known orphan and is swept.
        """
        if now > leg.deadline + self.cfg.intent_grace_s:
            return True
        return leg.cancel_acked_at is not None and now - leg.cancel_acked_at >= CANCEL_CONFIRM_S

    def _leg_vanished(self, r: Round, leg: Leg) -> None:
        """A filled position disappeared. Ours if a close covers it; otherwise liquidation or external."""
        ours = any(leg.position_id in c.position_ids for c in r.closes)
        trade = self._trade(leg.position_id)
        liquidated = trade is not None and trade.liquidated
        leg.status = LegStatus.LIQUIDATED.value if liquidated else LegStatus.CLOSED.value
        if ours and not liquidated:
            return
        if liquidated:
            r.liquidated = True
            self._event("leg_liquidated", r, side=leg.side, position_id=leg.position_id,
                        note="losing leg liquidated; acceptable outcome, closing the remaining leg")
        else:
            self._event("leg_vanished", r, level="warning", side=leg.side, position_id=leg.position_id,
                        note="position gone without our close; closing the remaining leg")
        if r.state != RoundState.CLOSING.value:
            self._to_closing(r, "loser_liquidated" if liquidated else "leg_vanished")

    def _to_closing(self, r: Round, reason: str) -> None:
        r.state = RoundState.CLOSING.value
        r.close_reason = reason
        r.close_started_at = self.clock()
        self._event("closing", r, reason=reason, hedged=r.became_hedged)

    def _step_opening(self, r: Round, now: float) -> None:
        legs = r.legs
        filled = [l for l in legs if l.status == LegStatus.FILLED.value]
        pending = [l for l in legs if l.status == LegStatus.PENDING.value]
        dead = [l for l in legs if l.status == LegStatus.DEAD.value]
        elapsed = now - (r.submitted_at or r.created_at)

        if len(filled) == 2:
            a, b = filled
            if a.is_long == b.is_long or abs((a.margin_usd or 0) - (b.margin_usd or 0)) > 0.01:
                self._to_closing(r, "pair_mismatch")
                return
            r.state = RoundState.HEDGED.value
            r.became_hedged = True
            r.hedged_at = now
            r.entry_ref = (a.entry_price + b.entry_price) / 2
            self._event("hedged", r, long_entry=r.long.entry_price, short_entry=r.short.entry_price,
                        fill_spread=abs(r.long.entry_price - r.short.entry_price) / r.entry_ref)
            return

        if filled:
            reason = None
            if dead:
                reason = "other_leg_dead"
            elif pending and elapsed >= self.cfg.leg_fill_timeout_s:
                reason = "leg_fill_timeout"
            elif self.shutting_down:
                reason = "shutdown"
            if reason:
                for leg in pending:
                    self._cancel(r, leg)
                self.notifier.alert("one_leg_unwind", f"round {r.id} {r.market}: only the {filled[0].side} leg filled ({reason}); closing it",
                                    dedupe_key=f"unwind:{r.id}")
                self._to_closing(r, reason)
            return

        if not pending:
            self._finalize(r, now)
            return
        if dead or elapsed >= self.cfg.open_abandon_timeout_s or self.shutting_down:
            for leg in pending:
                self._cancel(r, leg)

    def _step_hedged(self, r: Round, ctx: MarketContext | None, now: float) -> None:
        if self.shutting_down and self.cfg.flatten_hedged_on_shutdown:
            self._to_closing(r, "shutdown_flatten")
            return
        mid = ctx.mids.get(r.market) if ctx else None
        if mid is None or r.entry_ref is None:
            return
        move = (mid - r.entry_ref) / r.entry_ref
        loser = r.short if move > 0 else r.long
        bust_distance = abs(loser.bust_price - mid) / mid if loser.bust_price else 1.0
        emergency = r.emergency_distance if r.emergency_distance is not None else self.cfg.emergency_bust_distance
        if abs(move) >= r.tp_move:
            self._to_closing(r, "take_profit")
        elif bust_distance <= emergency:
            self._to_closing(r, "emergency_bust_distance")

    def _step_closing(self, r: Round, account: AccountSnapshot, ctx: MarketContext | None, now: float) -> None:
        for leg in r.legs:
            if leg.status == LegStatus.PENDING.value:
                self._cancel(r, leg)
        open_ids = [leg.position_id for leg in r.legs
                    if leg.status == LegStatus.FILLED.value and account.position(leg.position_id) is not None]
        r.closes = self._manage_closes(r.closes, open_ids, account, now, r.id, "close")
        if open_ids or any(l.status == LegStatus.PENDING.value for l in r.legs):
            return
        r.positions_gone_at = r.positions_gone_at or now
        self._finalize(r, now)

    # ---------------------------------------------------------- close manager
    def _manage_closes(self, closes: list[CloseRecord], open_ids: list[int], account: AccountSnapshot, now: float,
                       round_id: int | None, purpose: str) -> list[CloseRecord]:
        """Keep exactly enough live close intents to cover every open position, re-signing before expiry."""
        open_set = set(open_ids)
        for c in closes:
            if c.status not in ("accepted", "unconfirmed"):
                continue
            remaining = open_set & set(c.position_ids)
            if not remaining:
                c.status = "done"
            elif now > c.deadline + self.cfg.intent_grace_s:
                c.status = "dead"
                self.store.set_intent_status(c.intent_id, "dead")
            elif account.operation(c.intent_id) is None and now - c.submitted_at > CLOSE_MISSING_S:
                c.status, c.code = "dead", "missing_from_queue"
                self.store.set_intent_status(c.intent_id, "dead", "missing_from_queue")
        live = [c for c in closes if c.status in ("accepted", "unconfirmed")]
        covered = {pid for c in live for pid in c.position_ids}

        if not self.breaker.can_submit():
            return closes
        for c in live:
            if c.code == "resign_blocked" or now < c.deadline - self.cfg.close_resign_before_expiry_s:
                continue
            ids = sorted(open_set & set(c.position_ids))
            result = self._submit(self.venue.prepare_close(ids), round_id, purpose + "_resign")
            if result.status is SubmitStatus.REJECTED:
                c.code = "resign_blocked"
            else:
                c.status = "superseded"
                closes.append(CloseRecord(result.intent_id, ids, self._deadline_of(result), now, result.status.value, result.code))
            self.store.log_event("close_resigned", round_id=round_id, old=c.intent_id, new=result.intent_id, status=result.status.value)

        uncovered = sorted(open_set - covered)
        for i in range(0, len(uncovered), MAX_CLOSE_IDS):
            ids = uncovered[i:i + MAX_CLOSE_IDS]
            prepared = self.venue.prepare_close(ids)
            result = self._submit(prepared, round_id, purpose)
            closes.append(CloseRecord(result.intent_id, ids, prepared.deadline, now, result.status.value, result.code))
            if result.status is SubmitStatus.REJECTED:
                closes[-1].status = "dead"
        return closes

    def _deadline_of(self, result: SubmitResult) -> int:
        row = self.store.intent(result.intent_id)
        return int(row["deadline"]) if row else int(self.clock()) + 3600

    # -------------------------------------------------------------- settlement
    def _finalize(self, r: Round, now: float) -> None:
        missing = []
        for leg in r.legs:
            if leg.position_id is None:
                continue
            trade = self._trade(leg.position_id)
            if trade is None:
                missing.append(leg.position_id)
                continue
            leg.exit_price, leg.pnl_usd, leg.fee_usd = trade.exit_price, trade.pnl_usd, trade.fee_usd
            leg.paper_minted, leg.paper_basis_usd = trade.paper_minted, trade.paper_mint_basis_usd
            leg.status = LegStatus.LIQUIDATED.value if trade.liquidated else LegStatus.CLOSED.value
            r.liquidated = r.liquidated or trade.liquidated
        if missing and now - (r.positions_gone_at or now) < self.cfg.settle_grace_s:
            return
        if missing:
            r.notes.append(f"trade history missing for positions {missing}; PnL incomplete")

        r.net_pnl_usd = sum(leg.net_pnl_usd for leg in r.legs)
        r.paper_minted = sum(leg.paper_minted for leg in r.legs)
        r.cost_per_paper = -r.net_pnl_usd / r.paper_minted if r.paper_minted > 0 else None
        r.state = RoundState.DONE.value if r.became_hedged else RoundState.ABORTED.value
        self._record_close_quality(r)
        self._mint_cross_check(r)
        self._save(r, closed=True)
        self._event("round_closed", r, outcome=r.state, reason=r.close_reason, liquidated=r.liquidated,
                    long_entry=r.long.entry_price, long_exit=r.long.exit_price, long_pnl=r.long.net_pnl_usd,
                    short_entry=r.short.entry_price, short_exit=r.short.exit_price, short_pnl=r.short.net_pnl_usd,
                    net_pnl=r.net_pnl_usd, paper_minted=r.paper_minted, cost_per_paper=r.cost_per_paper,
                    expected_cost_per_paper=r.expected_cost_per_paper, tranche=r.tranche,
                    close_latency_s=r.close_latency_s, close_mode=r.close_mode, tp_move=r.tp_move, leverage=r.leverage,
                    exit_move=r.exit_move, overshoot_fraction=r.overshoot_fraction, notes=r.notes)
        if self.last_account is not None:
            self._maybe_stake(self.last_account, self.last_ctx, now, force=True)

    def _mint_cross_check(self, r: Round) -> None:
        ctx = self.last_ctx
        if ctx is None:
            return
        model = ctx.spot_mint_rate
        for leg in r.legs:
            if leg.paper_basis_usd > 1 and leg.paper_minted > 0:
                observed = leg.paper_minted / leg.paper_basis_usd
                if observed < model * (1 - MINT_CHECK_TOLERANCE):
                    self.halt("mint_curve_mismatch", f"observed {observed:.2f} PAPER/$ vs model {model:.2f} on position {leg.position_id}")
                elif observed > model * (1 + MINT_CHECK_TOLERANCE):
                    self.notifier.alert("mint_rate_above_model", f"observed {observed:.2f} PAPER/$ vs model {model:.2f}", level="info")

    # ------------------------------------------------------------------ sweep
    def _sweep_orphans(self, account: AccountSnapshot, claimed: set[int], now: float) -> None:
        state = self.store.kv_get(SWEEP_KEY, {"closes": [], "positions": {}})
        closes = [CloseRecord(**c) for c in state["closes"]]
        tracked: dict[str, dict] = state["positions"]
        for pos in account.positions:
            if pos.position_id in claimed or str(pos.position_id) in tracked:
                continue
            known = pos.intent_id is not None and self.store.intent(pos.intent_id) is not None
            tracked[str(pos.position_id)] = {"intent_id": pos.intent_id, "known": known, "first_seen": now}
            self.notifier.alert("orphan_position", f"position {pos.position_id} ({'ours, late fill' if known else 'unknown origin'}) "
                                f"is not part of an active hedge", level="error", dedupe_key=f"orphan:{pos.position_id}")
            self.store.log_event("orphan_detected", level="error", position_id=pos.position_id, intent_id=pos.intent_id, known=known)
        open_ids = [int(pid) for pid in tracked if account.position(int(pid)) is not None]
        to_close = [pid for pid in open_ids if tracked[str(pid)]["known"] or self.cfg.unknown_position_policy == "close"]
        if len(to_close) < len(open_ids):
            self.halt("unknown_position", "unknown open positions present and UNKNOWN_POSITION_POLICY=halt")
        closes = self._manage_closes(closes, to_close, account, now, None, "sweep")
        for pid in [p for p in list(tracked) if account.position(int(p)) is None]:
            trade = self._trade(int(pid))
            if trade is None and now - tracked[pid]["first_seen"] < self.cfg.settle_grace_s:
                continue
            self.store.log_event("sweep_closed", position_id=int(pid), net_pnl=trade.net_pnl_usd if trade else None,
                                 paper_minted=trade.paper_minted if trade else None, liquidated=trade.liquidated if trade else None)
            del tracked[pid]
        closes = [c for c in closes if c.status in ("accepted", "unconfirmed")]
        self.store.kv_set(SWEEP_KEY, {"closes": [c.__dict__ for c in closes], "positions": tracked})

    def sweep_active(self) -> bool:
        return bool(self.store.kv_get(SWEEP_KEY, {"positions": {}})["positions"])

    # ---------------------------------------------------------------- staking
    def _maybe_stake(self, account: AccountSnapshot, ctx: MarketContext | None, now: float, force: bool = False) -> None:
        if self.staker.mode == "off" or (not force and now - self._last_stake_at < STAKE_MIN_INTERVAL_S):
            return
        minimum = ctx.trading.paper_stake_minimum if ctx else 1000.0
        try:
            amount = self.staker.after_round(account, minimum)
        except Exception as exc:  # staking must never break the trading loop
            self.notifier.alert("stake_error", f"staking module error: {exc}", level="error")
            return
        self._last_stake_at = now
        if amount:
            self._event("paper_staked", amount=amount, mode=self.staker.mode)

    # ------------------------------------------------------------ new rounds
    def loss_budget(self, now: float, active: list[Round]) -> LossBudget:
        return budget.daily_loss_budget(self.cfg, self.store, self.last_ctx, now, active)

    def tranche_usage(self, active: list[Round]) -> dict[str, TrancheUsage]:
        return budget.tranche_usage(self.cfg, self.store, self.last_ctx, active)

    def _record_close_quality(self, r: Round) -> None:
        """Track slow or overshooting closes: in vol_matched mode lower leverage first, then pause new rounds."""
        if r.close_started_at is None or r.positions_gone_at is None or not r.closes:
            return
        cfg = self.cfg
        r.close_latency_s = r.positions_gone_at - r.close_started_at
        r.exit_move, r.overshoot_fraction = close_overshoot(r)
        slow = r.close_latency_s > cfg.close_latency_alert_s
        overshot = r.overshoot_fraction is not None and r.overshoot_fraction > cfg.close_overshoot_alert_fraction
        state = {**CLOSE_QUALITY_DEFAULT, **self.store.kv_get(CLOSE_QUALITY_KEY, {})}
        if not (slow or overshot):
            state["slow_streak"] = 0
            state["good_streak"] += 1
            if state["derate_steps"] and state["good_streak"] >= DERATE_RECOVERY_CLOSES:
                state["derate_steps"] -= 1
                state["good_streak"] = 0
                self.notifier.alert("leverage_restored", f"{DERATE_RECOVERY_CLOSES} clean closes; leverage derate now "
                                    f"{state['derate_steps']} step(s)", level="info", cooldown_s=0)
            self.store.kv_set(CLOSE_QUALITY_KEY, state)
            return

        state["good_streak"] = 0
        state["slow_streak"] += 1
        what = (f"last close took {r.close_latency_s / 60:.1f} min"
                + (f", overshot by {r.overshoot_fraction:.0%} of the take-profit-to-liquidation gap" if overshot else ""))
        if state["slow_streak"] >= cfg.close_latency_alert_count:
            if cfg.close_mode == VOL_MATCHED and state["derate_steps"] < cfg.leverage_derate_max_steps:
                state["derate_steps"] += 1
                state["slow_streak"] = 0
                self.notifier.alert("leverage_derated", f"{cfg.close_latency_alert_count} consecutive slow/overshooting closes "
                                    f"({what}); lowering leverage (derate step {state['derate_steps']}/"
                                    f"{cfg.leverage_derate_max_steps})", level="warning", cooldown_s=0)
            else:
                state["paused_until"] = self.clock() + cfg.close_latency_pause_s
                self.notifier.alert("slow_closes", f"{state['slow_streak']} consecutive closes slower than "
                                    f"{cfg.close_latency_alert_s / 60:.0f} min or overshooting ({what}); "
                                    f"new rounds paused {cfg.close_latency_pause_s / 60:.0f} min", level="error", cooldown_s=0)
        self.store.kv_set(CLOSE_QUALITY_KEY, state)

    def _report_gates(self, market: str, blocked_now: list) -> None:
        names = {g.name for g in blocked_now}
        before = self.blocked.get(market, set())
        for g in blocked_now:
            if g.name not in before:
                kind, level = GATE_ALERTS.get(g.name, (f"gate_{g.name}", "warning"))
                self.notifier.alert(kind, f"{market}: new rounds paused: {g.detail}", level=level, dedupe_key=f"{kind}:{market}",
                                    cooldown_s=0)
        for name in before - names:
            self.notifier.alert("gate_cleared", f"{market}: {name} gate cleared", level="info", dedupe_key=f"clear:{market}:{name}", cooldown_s=0)
        self.blocked[market] = names

    def _maybe_start_rounds(self, ctx: MarketContext, account: AccountSnapshot, now: float, active: list[Round]) -> None:
        if self.halted() or self.sweep_active():
            return
        for market in self.cfg.markets:
            if any(r.market == market for r in active):
                continue
            last = self.store.last_closed_at(market)
            if last is not None and now - last < self.cfg.round_cooldown_s:
                continue
            self._report_shape(market, ctx)
            paused_until = self.store.kv_get(CLOSE_QUALITY_KEY, {}).get("paused_until", 0.0)
            plan = plan_round(self.cfg, ctx, market, account, self.breaker, self.loss_budget(now, active),
                              self.session_address, self.tranche_usage(active), paused_until)
            blocked_now = blocking(plan.gates, self.cfg.dry_run_force_rounds)
            self._report_gates(market, blocked_now)
            if blocked_now:
                continue
            active.append(self._start_round(market, ctx, plan.sizing, now, plan.tranche.name if plan.tranche else None,
                                            plan.shape))

    def _report_shape(self, market: str, ctx: MarketContext) -> None:
        shape = ctx.shapes.get(market)
        if shape is None or shape.mode != VOL_MATCHED:
            return
        if shape.sigma_source == "unavailable":
            self.notifier.alert("volatility_unavailable", f"{market}: no recent Hyperliquid candles; using the widest close "
                                f"{shape.tp_move:.2%} at {shape.leverage}x", level="warning", dedupe_key=f"vol:{market}", cooldown_s=3600)
        behind = bool(ctx.pace and ctx.pace.behind)
        was_behind = self.store.kv_get(PACE_KEY, {}).get("behind", False)
        if behind != was_behind:
            self.store.kv_set(PACE_KEY, {"behind": behind, "ts": ctx.now})
            if behind:
                self.notifier.alert("behind_pace", f"{ctx.pace.describe()}; tightening close to {shape.tp_move:.2%} at "
                                    f"{shape.leverage}x", level="warning", cooldown_s=0)
            else:
                self.notifier.alert("pace_restored", f"{ctx.pace.describe() if ctx.pace else 'no pace schedule'}", level="info", cooldown_s=0)

    def _start_round(self, market: str, ctx: MarketContext, sizing, now: float, tranche: str | None = None,
                     shape: CloseShape | None = None) -> Round:
        instrument_id = INSTRUMENTS[market]
        margin_wad = int(round(sizing.margin_usd * 100)) * 10**16
        est = ctx.costs[market]
        tp_move = shape.tp_move if shape else self.cfg.take_profit_move
        round_id = self.store.insert_round(market, RoundState.OPENING.value, {})
        p_long = self.venue.prepare_open(instrument_id, True, margin_wad, sizing.leverage)
        p_short = self.venue.prepare_open(instrument_id, False, margin_wad, sizing.leverage)
        r = Round(
            id=round_id, market=market, instrument_id=instrument_id, state=RoundState.OPENING.value, created_at=now,
            margin_usd=sizing.margin_usd, leverage=sizing.leverage, tp_move=tp_move,
            long=Leg("long", p_long.intent_id, p_long.deadline), short=Leg("short", p_short.intent_id, p_short.deadline),
            tranche=tranche, expected_cost_per_paper=est.cost_per_paper, mint_rate_at_open=est.spot.mint_rate,
            submitted_at=now,
        )
        if shape is not None:
            r.close_mode, r.emergency_distance, r.liq_move = shape.mode, shape.emergency_distance, shape.liq_move
            r.sigma_hourly, r.behind_pace = shape.sigma_hourly, shape.behind_pace
        for p, purpose in ((p_long, "open_long"), (p_short, "open_short")):
            self.store.record_intent(p.intent_id, round_id, purpose, p.action, p.nonce, p.deadline, p.signed)
        self._save(r)
        self._event("round_opened", r, tranche=tranche, margin=sizing.margin_usd, leverage=sizing.leverage, notional=sizing.notional_usd,
                    tp_move=tp_move, close_mode=r.close_mode, liq_move=r.liq_move, sigma_hourly=r.sigma_hourly,
                    behind_pace=r.behind_pace, expected_cost_per_paper=est.cost_per_paper, mint_rate=est.spot.mint_rate)

        first = self._submit(p_long, round_id, "open_long", record=False)
        self._apply_submit(r.long, first)
        if first.status is SubmitStatus.ACCEPTED:
            self._apply_submit(r.short, self._submit(p_short, round_id, "open_short", record=False))
        else:
            r.short.status = LegStatus.DEAD.value
            r.short.submit_status = "not_submitted"
            self.store.set_intent_status(p_short.intent_id, "not_submitted")
        self._save(r)
        return r

    @staticmethod
    def _apply_submit(leg: Leg, result: SubmitResult) -> None:
        leg.intent_id = result.intent_id
        leg.submit_status = result.status.value
        if result.status is SubmitStatus.REJECTED:
            leg.status = LegStatus.DEAD.value

    # --------------------------------------------------------------- shutdown
    def exposure_summary(self) -> list[str]:
        out = []
        for r in self.active_rounds():
            if r.state != RoundState.HEDGED.value or self.cfg.flatten_hedged_on_shutdown:
                out.append(f"round {r.id} {r.market} {r.state}")
        if self.sweep_active():
            out.append("orphan sweep in progress")
        return out

    def safe_to_exit(self) -> bool:
        return not self.exposure_summary()

    def status(self) -> dict:
        halt = self.halted()
        return {
            "mode": self.cfg.mode,
            "halted": halt,
            "blocked": {m: sorted(v) for m, v in self.blocked.items()},
            "active_rounds": [{"id": r.id, "market": r.market, "state": r.state} for r in self.active_rounds()],
            "relayer_failures": self.breaker.failures,
            "last_tick_ok": self.last_tick_ok,
            "shutting_down": self.shutting_down,
        }
