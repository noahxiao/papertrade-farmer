# papertrade-farmer

An unattended, hedged PAPER farmer for [Papertrade](https://exchange.papertrade.xyz).

Each round opens an equal BTC long and short, waits until both are confirmed
filled, closes **both legs in one Close order** once the mid has moved the
take-profit distance, optionally stakes the PAPER minted on the losing leg,
and repeats. Stability and safety come before features: it starts in a
simulation mode, refuses to hold a naked side, and stops on anything it does
not recognise.

> Papertrade's relayer API is undocumented. Everything here was decoded from
> the live frontend bundle and public read endpoints. **No real trade has been
> placed with this code.** See [Verified vs assumed](#verified-vs-assumed).

## Setup

```bash
python3.12 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # safe defaults: DRY_RUN, conservative limits
python -m ptfarmer check-config # validates and prints the config, secrets redacted
python -m ptfarmer monitor      # read-only report from public feeds, no keys needed
python -m ptfarmer run          # the service (DRY_RUN simulation unless live flags are set)
python -m pytest -q             # test suite (mocked relayer, no network)
```

| Command | What it does |
| --- | --- |
| `monitor [--json] [--watch N] [--skip-chain]` | Protocol state, cost per PAPER, value-side metrics, account, budgets, next-round gates and rule checks. Never signs anything. |
| `run` | The farmer loop (see [Service operation](#service-operation)). |
| `summary [--date YYYY-MM-DD] [--notify]` | Daily summary; `--notify` also posts it to `WEBHOOK_URL`. |
| `healthcheck [--max-age S]` | Exit 0 if the heartbeat is fresh, 1 if stale, 2 if missing. |
| `status` | Halt latch, active rounds, orphan sweep and close-latency state. |
| `resume` | Clears a latched halt after you have reviewed why it happened. |
| `check-config` | Validates configuration (exit 3 on error). |

## Modes and flags

* **DRY_RUN (default).** A simulator (`SimVenue`) replays the relayer queue
  and settlement against live Hyperliquid mids and the live mint rate. No
  request is ever sent to the relayer. `DRY_RUN_FORCE_ROUNDS=true` lets the
  simulation trade even when the cost and value-side gates say no.
* **LIVE.** Requires all three of `DRY_RUN=false`, `LIVE=true` and
  `LIVE_CONFIRM=I_UNDERSTAND_THIS_TRADES_REAL_FUNDS`, plus `WALLET_ADDRESS`
  and `SESSION_KEY`. Any partial combination is a configuration error, not a
  silent fallback.
* **Monitor.** `python -m ptfarmer monitor` is read-only in every mode.

Secrets live only in `.env` (gitignored) or the process environment. They are
redacted in logs, `check-config`, and the heartbeat file.

## Safety model

**Never a naked side.** The account snapshot (open positions plus pending
relayer operations) is the source of truth, re-read every loop and on
startup. If it cannot be read, the loop takes no action at all.

* Both open intents are written to SQLite *before* submission. The short is
  only sent if the long was accepted.
* A round becomes HEDGED only when both positions are seen on the account
  with matching side and margin.
* If one leg fills and the other is rejected, expires, is not filled within
  `LEG_FILL_TIMEOUT_S`, or the process is shutting down, the pending leg is
  cancelled and the filled leg is closed. The round is then skipped.
* A pending leg counts as dead only when the relayer rejected it, its
  deadline plus `INTENT_GRACE_S` has passed, or a cancel was acknowledged
  and the intent has been absent from the queue for 2 minutes.
* **Late fills.** A leg that fills after its round gave up is closed by the
  round. A fill after the round has finished is caught by the orphan sweeper.
  Any open position not owned by an active round is an orphan. Orphans the
  bot created are always closed. Unknown ones are closed, or halt the bot,
  per `UNKNOWN_POSITION_POLICY`. New rounds wait until the sweep is done.
* **A liquidated losing leg is acceptable at 40x.** It is logged, the
  winner is closed, and farming continues.

**Relayer queue.** Every submission resolves to one of three outcomes:

* *accepted*.
* *rejected*: a definite error code, so it was never queued.
* *unconfirmed*: a timeout, 5xx or unknown error. It may still land, so it is
  treated as live until its deadline.

Close orders are re-signed `CLOSE_RESIGN_BEFORE_EXPIRY_S` before their 1h
expiry. If the relayer refuses a second close while the first is pending,
the bot waits for the first one to expire and then resubmits. A close missing
from the queue for 2 minutes is resubmitted. Transport failures trigger
exponential back-off (5s to 300s, honouring `Retry-After`).
`/relayer/health` not ready, or `RELAYER_MAX_CONSECUTIVE_FAILURES` failures
in a row, pause new rounds. An outage longer than `RELAYER_DOWN_HALT_S`
raises an alert. Open rounds are still managed throughout.

**Entry gates.** Each round is checked before it opens. *Pause* gates clear
by themselves; *halt* is latched until `ptfarmer resume`.

| Gate | Default | Effect |
| --- | --- | --- |
| Market open / intake not paused | always | pause |
| IOU payout queue empty | always | pause (open rounds still close) |
| Relayer healthy and not backing off | always | pause; alert after `RELAYER_DOWN_HALT_S` |
| Session key registered and valid for more than 2h (live) | always | pause |
| Cost per PAPER (includes expected mint-rate decay) | `MAX_COST_PER_PAPER=0.0016` | pause; `off` disables |
| Daily loss: realized today + open worst case + next worst case | `DAILY_LOSS_CAP_USD=100` | pause; cannot be disabled |
| Budget tranche available (see below) | none configured | pause |
| LP floor: LP below $0.5M, or trending below it within `LP_TREND_HORIZON_H` | `LP_FLOOR_USD=500000` | pause |
| Mint rate below 20 and no surplus flowing to stakers | `MINT_RATE_STOP=20` | pause |
| Closes repeatedly slower than ~5 min | `CLOSE_LATENCY_ALERT_S=300`, `_COUNT=2` | alert, pause `CLOSE_LATENCY_PAUSE_S` |
| Size: protocol minimums, max position, OI-cap headroom | always | pause |
| Value side: fee yield, payback days, LP vs cap, LP trend | off | pause (advisory) |
| Rule watch (below) | always | **halt** |
| Mint observed on our own closes more than 10% below the model | always | **halt** |

Rounds are **sized to fit the budget**. A round is shrunk so that its
worst case (`ROUND_COST_SAFETY_FACTOR` × the loss if the losing leg is
liquidated and the winner is closed at the same price) fits both the
remaining daily cap and its tranche. It is blocked only when even a
protocol-minimum round would not fit. Cost per PAPER does not depend on
size in this model, so smaller rounds lose no efficiency. Budgets get spent
fully, and the projection never overruns them.

**Rule watch.** Halts and alerts when any of these drift:

* the mint curve constants or the staker-cap schedule;
* the minimums or the fee and bust-buffer parameters;
* proxy implementation or admin, or the bytecode of the four contracts;
* the EIP-712 type definitions, domain names, intent-id tag, contract
  addresses or chain id in the live frontend.

These are pinned in `ptfarmer/baseline.json`. The keeper-managed OI caps
are deliberately not pinned (they move); a headroom check is used instead.

**Graceful shutdown.** On SIGTERM or SIGINT the bot stops opening rounds,
cancels pending legs and closes any single leg, then exits. Hedged pairs are
left open for the next process to resume, unless
`FLATTEN_HEDGED_ON_SHUTDOWN=true`. If `SHUTDOWN_GRACE_S` runs out with
work still pending, it sends a critical alert. Only one farmer can run per
data directory (file lock), because a second engine would sweep the first
one's legs as orphans.

## Strategy parameters

| Flag | Default | Notes |
| --- | --- | --- |
| `LEVERAGE` | 40 | Hard cap 100. |
| `TAKE_PROFIT_MOVE_PCT` | 1.0 | Sensible range 0.75–1. Must be ≤ `TP_MAX_FRACTION_OF_LIQ` (0.60) × the liquidation distance (2.45% at 40x). |
| `EMERGENCY_BUST_DISTANCE_PCT` | 0.30 | Close early if the loser gets this close to bust. |
| `MARGIN_PER_LEG_USD` | 250 | With `SIZE_FROM_BALANCE=true`, capped at (available − `BALANCE_BUFFER_USD`) / 2. |
| `ENABLE_ETH` | false | ETH is optional. |
| `ROUND_COOLDOWN_S` | 60 | 0 = start the next round right after a close (gates permitting). |
| `STAKE_MODE` | remind | `off`, `remind` (alerts when PAPER is unstaked), or `auto` (stakes after every round; see custody below). |

The full list, with comments, is in [`.env.example`](.env.example).

### Profiles

* **`.env.example`**: safe defaults. At current state (mint rate ~40/$,
  falling ~2.3%/h) the all-in cost is ~$0.0035–0.0038 per PAPER. That is
  above the 0.0016 limit, so **the default bot sits paused** and only the
  monitor and simulation do anything.
* **[`profiles/max-paper.env`](profiles/max-paper.env)**: the owner's
  maximum-staked-PAPER profile:
  * BTC only, 40x, closed at a 1% move;
  * $2,750 margin per leg, sized from balance;
  * `MAX_COST_PER_PAPER=off`, a $1,000 daily cap, no cooldown;
  * auto-stake, and the tranche schedule in
    [`profiles/tranches.example.json`](profiles/tranches.example.json).

  It still starts in DRY_RUN. It uses `ROUND_COST_SAFETY_FACTOR=1.25`, so
  a full-size round fits a fresh $500 tranche; the file explains the
  trade-off.

### Budget tranches

`BUDGET_TRANCHES` (inline JSON) or `BUDGET_TRANCHES_FILE` holds a list of
lifetime loss budgets:

```json
[{"name": "tranche1", "budget_usd": 500},
 {"name": "tranche2", "budget_usd": 500, "available_until": "2026-10-17T18:45:00Z"},
 {"name": "reserve",  "budget_usd": 300, "kind": "opportunistic"}]
```

Each round draws from the first *standard* tranche that:

* is inside its window;
* has a mint rate ≥ its `min_mint_rate` (default `MIN_MINT_RATE=28`);
* is open while the payout queue is empty;
* can still absorb a minimum-size round.

A tranche is spent by its rounds' realized net losses plus the worst case of
its open rounds.

The *opportunistic* tranche is only used when every standard tranche is
unavailable, the payout queue is empty, and tracked LP is between
`OPPORTUNISTIC_LP_MIN_USD` ($0.5M) and the $2M flat-rate line. That is the
"flat 100 PAPER/$" region. Its rounds are small
(`OPPORTUNISTIC_MARGIN_PER_LEG_USD`, 0 = protocol minimum). The daily cap
applies on top of all tranches.

## Logging and reporting

Everything is stored in `DATA_DIR/farmer.sqlite3` (WAL, synchronous=FULL),
in these tables:

* rounds
* write-ahead intents
* events and alerts
* stakes
* protocol samples
* nonces

Rounds and events are also appended to `rounds.jsonl` and `events.jsonl`.

The **monitor** and the **daily summary** (posted automatically after
`DAILY_SUMMARY_HOUR_UTC`) show:

* mint rate, flat or ratchet region, and decay per hour;
* LP against the $2M flat-rate line and the $5M and $10M surplus caps
  (switching at 2026-10-17 18:45 UTC);
* LP trend and payout-queue status;
* staker yield per PAPER per day and its trend, and payback days;
* PAPER minted and staked, staked balance, and claimable rewards;
* daily budget and tranche budget used and remaining;
* close latency and the alert counts.

`WEBHOOK_URL` takes a Slack- or Discord-compatible incoming webhook.

## Service operation

* `deploy/papertrade-farmer.service` is a systemd unit:
  `Restart=always`, SIGTERM with `TimeoutStopSec` above the shutdown
  grace, and sandboxing.
* `deploy/papertrade-farmer-healthcheck.{service,timer}` restarts the
  farmer when its heartbeat goes stale (a hung loop rather than a crash).
* `deploy/supervise.sh` is a supervisor for hosts without systemd. It
  restarts on exit or a stale heartbeat, and forwards SIGTERM.
* The heartbeat is `DATA_DIR/heartbeat.json`, rewritten atomically every
  loop. It holds the mode, halt state, blocked gates, active rounds and
  relayer failures.

## Verified vs assumed

Sources:

* frontend build `7976669c6bab`, with the typed-data, deployment and
  build chunks re-checked by the rule watcher every 15 minutes;
* public read endpoints;
* HyperEVM RPC;
* the public trade history of leaderboard accounts (read locally only;
  nothing about them is stored in this repo).

**Verified (observed or reproduced):**

* EIP-712 typed fields of `OpenPosition`, `Close`, `CancelIntent` and
  `Stake`. They are extracted live from the frontend and must match exactly.
* Domain names "String" (exchange) and "PaperTrade Intent Cancel", the
  intent-id tag string, chain id 999, and the contract addresses.
* Contract bytecode, proxy admin and implementations (on chain).
* The read API, used live:
  * `/query/protocol/summary`, the queue summary and `/state/trading`
    (the 23-field instrument tuple);
  * `/query/protocol/history`, `/relayer/health`, and
    `/query/accounts/{a}/trades?limit=75`;
  * the account SSE stream `GET /state/user/live?wallet=` (first full
    wallet frame).
* Economics, reproduced on real closes:
  * a win fee of 2% of the gain;
  * the win-side impact scale (1 − base) / (1 + 1/(m·RM) + ref/(1e6·m·PM))
    after a 1/50000 deadband;
  * a loss-side mint basis of loss × 0.98 while the queue is empty;
  * a ratchet mint rate of 100·(120M/(120M+tail))².
* Our signatures recover to the signer, and our wire format reproduces the
  frontend's (strings, `v` as "27"/"28").

**Decoded from the bundle but never exercised (no live submission was made):**

* `POST /intents {action, signed}` returns `{intentId, action, state,
  queueRevision}`. Cancel is `POST /intents/{id}/cancel`.
* The `X-Papertrade-Build` header.
* The definite rejection codes (`invalid_intent`, `deadline_expired`,
  `admission_rejected`, `intent_conflict`, `rate_limited`, …).
* The client-chosen nonce scheme (random × 256, then incremented, per route
  group), and `size` = margin in WAD.

**Assumed or inferred:**

* `intentId` = keccak(keccak(tag) ‖ digest ‖ signer). The relayer's
  returned id is treated as authoritative, and a mismatch is logged.
* The cancel domain's `verifyingContract` is the exchange.
* Origin and Referer headers are needed.
* What the relayer does with a second Close for positions that already have
  one pending. Both outcomes are handled.
* Whether an acknowledged cancel can still lose to a fill. The sweeper
  handles it.
* The flat 100/$ rate when LP is below $2M (from the docs; not observed in
  this state). The runtime mint cross-check halts if reality is lower.
* The claimable-rewards formula (MasterChef-style, 1e27 accumulator). It
  matched one account, whose value was 0, so this is weak evidence.
* **Session-key registration is not implemented.** The `RegisterSessionKey`
  fee address could not be resolved from the bundle, so register the
  session key in the app manually. The bot checks that it is registered and
  valid for more than 2h.

## Known risks

* **Undocumented API.** The rule watcher catches changes to typed data,
  contracts and parameters, but not purely behavioural relayer changes. The
  first live session should be supervised, with small size.
* **Economics.** Cost is ~$0.0034–0.0039 per PAPER, above the default
  0.0016 limit. Every round loses money by design; the PAPER is the product.
  Recent staker yield collapsed (about $0.0027 per PAPER per day over the last
  3h, trend sharply negative), so payback is uncertain.
* **Liquidation whipsaw.** If the price reverses after the loser busts and
  before the winner's close lands, the round can lose more than the modelled
  worst case. Realized losses always count against the caps.
* **Auto-staking custody.** `STAKE_MODE=auto` needs the wallet private key
  (`STAKE_WALLET_PRIVATE_KEY`), which can also sign withdrawals. Prefer
  `remind` unless the host is trusted. The key must belong to
  `WALLET_ADDRESS`, and it is refused unless the stake mode is `auto`.
* **Single instance per wallet.** The data-dir lock prevents two farmers on
  one host. It cannot stop two hosts using the same wallet.
* **Model inputs.** Decay and cycle time use configured volatility until
  enough samples exist. OI caps are keeper-dynamic.
