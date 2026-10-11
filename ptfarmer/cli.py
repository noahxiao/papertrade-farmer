"""Command-line entry point: `python -m ptfarmer <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone

from . import budget
from .app import build_farmer, build_readonly
from .config import ConfigError, load_config
from .feeds import FeedError
from .gates import plan_round
from .guards import RelayerBreaker, check_contracts, check_frontend, check_protocol_params
from .models import AccountSnapshot, FeedFormatError
from .reporting import daily_summary_text, monitor_json, monitor_report, summarize_day
from .service import Service, heartbeat_age_s

log = logging.getLogger("ptfarmer")


def _account(c) -> AccountSnapshot | None:
    if not c.cfg.wallet_address:
        return None
    try:
        return c.feeds.account_snapshot(c.cfg.wallet_address)
    except (FeedError, FeedFormatError) as exc:
        log.warning("account snapshot unavailable: %s", exc)
        return None


def _placeholder_account(cfg) -> AccountSnapshot:
    """Stand-in for gate evaluation when no wallet is configured: exactly enough balance for one round."""
    balance = 2 * cfg.margin_per_leg_usd + cfg.balance_buffer_usd
    return AccountSnapshot("0x" + "0" * 40, 0, 0, balance, 0.0, 0.0, 0.0, {}, (), ())


def cmd_monitor(args) -> int:
    cfg = load_config()
    c = build_readonly(cfg)
    while True:
        ctx = c.view.refresh()
        account = _account(c)
        findings = check_protocol_params(c.baseline, ctx.summary, ctx.trading, cfg.markets, ctx.now)
        if not args.skip_chain:
            findings += check_contracts(c.baseline, c.feeds)
            findings += check_frontend(c.baseline, c.feeds)[0]
        active = budget.active_rounds(c.store)
        loss = budget.daily_loss_budget(cfg, c.store, ctx, ctx.now, active)
        usage = budget.tranche_usage(cfg, c.store, ctx, active)
        breaker = RelayerBreaker(cfg.relayer_max_consecutive_failures, cfg.relayer_down_halt_s)
        breaker.record_health(ctx.relayer_ok)
        gates = []
        for market in cfg.markets:
            plan = plan_round(cfg, ctx, market, account or _placeholder_account(cfg), breaker, loss, None, usage)
            gates += [g.__class__(f"{market}.{g.name}", g.ok, g.action, g.detail, g.advisory) for g in plan.gates]
        if args.json:
            print(monitor_json(cfg, ctx, account, gates, findings))
        else:
            budget_line = (f"daily loss budget: ${loss.used_usd:,.2f} used of ${loss.cap_usd:,.2f}, "
                           f"${loss.remaining_usd:,.2f} remaining")
            print(monitor_report(cfg, ctx, c.baseline, account, gates, findings, usage, budget_line))
        if not args.watch:
            return 0
        print("-" * 80, flush=True)
        time.sleep(args.watch)


def cmd_run(args) -> int:
    cfg = load_config()
    c = build_farmer(cfg)
    log.info("starting %s with %s", cfg.mode, json.dumps(cfg.redacted(), default=str))
    return Service(c).run()


def cmd_summary(args) -> int:
    cfg = load_config()
    c = build_readonly(cfg)
    if args.date:
        day = datetime.strptime(args.date, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    else:
        day = budget.utc_day_start(time.time())
    ctx = None
    try:
        ctx = c.view.refresh()
    except (FeedError, FeedFormatError) as exc:
        log.warning("live context unavailable: %s", exc)
    active = budget.active_rounds(c.store)
    text = daily_summary_text(cfg, summarize_day(c.store, day), budget.tranche_usage(cfg, c.store, ctx, active), ctx,
                              _account(c), c.baseline, time.time())
    print(text)
    if args.notify:
        c.notifier.post(text)
    return 0


def cmd_healthcheck(args) -> int:
    cfg = load_config()
    age = heartbeat_age_s(cfg.heartbeat_path)
    limit = args.max_age or max(3 * cfg.poll_interval_s + 60, 120)
    if age is None:
        print(f"UNHEALTHY: no heartbeat at {cfg.heartbeat_path}")
        return 2
    data = json.loads(cfg.heartbeat_path.read_text())
    if age > limit:
        print(f"UNHEALTHY: heartbeat {age:.0f}s old (limit {limit:.0f}s)")
        return 1
    state = "HALTED" if data.get("halted") else "ok"
    print(f"{state}: heartbeat {age:.0f}s old, mode {data.get('mode')}, active rounds {len(data.get('active_rounds', []))}")
    return 0


def cmd_status(args) -> int:
    cfg = load_config()
    c = build_readonly(cfg)
    print(json.dumps({"halt": c.store.kv_get("halt"), "active_rounds": [r.to_dict() for r in budget.active_rounds(c.store)],
                      "sweep": c.store.kv_get("sweep"), "close_latency": c.store.kv_get("close_latency")}, indent=2, default=str))
    return 0


def cmd_resume(args) -> int:
    cfg = load_config()
    c = build_readonly(cfg)
    halt = c.store.kv_get("halt")
    if not halt:
        print("not halted")
        return 0
    c.store.kv_delete("halt")
    c.store.log_event("resumed", level="warning", previous=halt)
    print(f"cleared halt: {halt['reason']}: {halt['detail']}")
    return 0


def cmd_check_config(args) -> int:
    cfg = load_config()
    print(json.dumps(cfg.redacted(), indent=2, default=str))
    print(f"\nmode: {cfg.mode}; liquidation at {cfg.liquidation_move:.3%}; take-profit {cfg.take_profit_move:.3%} "
          f"({cfg.take_profit_move / cfg.liquidation_move:.0%} of liquidation distance)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ptfarmer", description="Hedged PAPER farmer for Papertrade (paper-safe by default)")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("monitor", help="read-only report from public feeds (no keys needed)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--watch", type=float, default=0, help="repeat every N seconds")
    p.add_argument("--skip-chain", action="store_true", help="skip bytecode and frontend checks")
    p.set_defaults(func=cmd_monitor)

    p = sub.add_parser("run", help="run the farmer service (DRY_RUN unless live flags are set)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("summary", help="print the daily summary")
    p.add_argument("--date", help="UTC date YYYY-MM-DD (default today)")
    p.add_argument("--notify", action="store_true", help="also post to WEBHOOK_URL")
    p.set_defaults(func=cmd_summary)

    p = sub.add_parser("healthcheck", help="exit non-zero if the heartbeat is stale")
    p.add_argument("--max-age", type=float, default=0)
    p.set_defaults(func=cmd_healthcheck)

    sub.add_parser("status", help="show halt state, active rounds and sweeps").set_defaults(func=cmd_status)
    sub.add_parser("resume", help="clear a latched halt after review").set_defaults(func=cmd_resume)
    sub.add_parser("check-config", help="validate configuration and print it (secrets redacted)").set_defaults(func=cmd_check_config)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 130
