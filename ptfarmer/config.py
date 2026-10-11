"""Configuration: environment / .env loading and validation.

Trading mode resolution is deliberately conservative:
  * DRY_RUN defaults to true.
  * Live trading needs DRY_RUN=false AND LIVE=true AND LIVE_CONFIRM set to the
    exact phrase below. Any partial combination is a hard configuration error,
    never a silent fallback in either direction.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .pace import Checkpoint, PaceError, parse_schedule, parse_utc
from .tranches import Tranche, TrancheError, load_tranches

LIVE_CONFIRM_PHRASE = "I_UNDERSTAND_THIS_TRADES_REAL_FUNDS"
DEFAULT_MAX_LEVERAGE = 100
# MAX_LEVERAGE may be raised explicitly up to this ceiling; take-profit must still fit inside liquidation.
ABSOLUTE_MAX_LEVERAGE = 200
CLOSE_MODES = ("fixed", "vol_matched")
CANDLE_INTERVALS = ("5m", "15m", "1h")
# Minimum distance between the take-profit and the liquidation price in vol_matched mode.
MIN_TP_LIQ_GAP = 0.002
SUPPORTED_MARKETS = ("BTC", "ETH")
STAKE_MODES = ("off", "remind", "auto")
UNKNOWN_POSITION_POLICIES = ("close", "halt")
# Bust buffer as read from /state/trading at launch; the runtime check uses the live value.
DEFAULT_BUST_BUFFER = 0.000476


class ConfigError(ValueError):
    pass


def load_dotenv(path: str | os.PathLike = ".env") -> dict[str, str]:
    """Minimal KEY=VALUE parser. Values already in the process env win."""
    values: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return values
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        values[key] = value
    return values


def _bool(env: dict[str, str], key: str, default: bool) -> bool:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    lowered = raw.strip().lower()
    if lowered in ("1", "true", "yes", "on"):
        return True
    if lowered in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{key} must be true/false, got {raw!r}")


def _float(env: dict[str, str], key: str, default: float) -> float:
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{key} must be a number, got {raw!r}") from exc


def _optional_limit(env: dict[str, str], key: str, default: float | None) -> float | None:
    """A numeric limit that can be disabled with off/none/disabled."""
    raw = env.get(key)
    if raw is None or raw == "":
        return default
    if raw.strip().lower() in ("off", "none", "disabled"):
        return None
    return _float(env, key, 0.0)


def _int(env: dict[str, str], key: str, default: int) -> int:
    value = _float(env, key, float(default))
    if value != int(value):
        raise ConfigError(f"{key} must be an integer, got {value}")
    return int(value)


def _str(env: dict[str, str], key: str, default: str = "") -> str:
    return (env.get(key) or default).strip()


_ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_KEY_RE = re.compile(r"^(0x)?[0-9a-fA-F]{64}$")


def liquidation_move(leverage: int, bust_buffer: float = DEFAULT_BUST_BUFFER) -> float:
    """Adverse mid move (fraction) at which a position busts."""
    return 1.0 / leverage - bust_buffer


@dataclass(frozen=True)
class Config:
    live: bool
    wallet_address: str
    session_key: str = field(repr=False)
    stake_wallet_key: str = field(repr=False)

    markets: tuple[str, ...]
    margin_per_leg_usd: float
    size_from_balance: bool
    balance_buffer_usd: float
    leverage: int
    take_profit_move: float
    tp_max_fraction_of_liq: float
    emergency_bust_distance: float

    max_cost_per_paper: float | None
    daily_loss_cap_usd: float
    round_cost_safety_factor: float
    round_cooldown_s: float

    leg_fill_timeout_s: float
    open_abandon_timeout_s: float
    intent_grace_s: float
    close_resign_before_expiry_s: float
    settle_grace_s: float
    relayer_down_halt_s: float
    relayer_max_consecutive_failures: int

    btc_daily_vol: float
    eth_daily_vol: float
    expected_fill_latency_h: float
    tail_growth_usd_per_h: float

    min_fee_yield_per_paper_day: float | None
    max_payback_days: float | None
    min_lp_fraction_of_cap: float | None
    require_lp_trend_nonnegative: bool

    tranches: tuple[Tranche, ...]
    min_mint_rate: float
    opportunistic_lp_min_usd: float
    opportunistic_margin_per_leg_usd: float
    lp_floor_usd: float | None
    lp_trend_horizon_h: float
    mint_rate_stop: float | None
    close_latency_alert_s: float
    close_latency_alert_count: int
    close_latency_pause_s: float

    stake_mode: str
    stake_min_paper: float
    unknown_position_policy: str
    flatten_hedged_on_shutdown: bool
    shutdown_grace_s: float
    dry_run_force_rounds: bool
    sim_fill_delay_s: float
    sim_start_balance_usd: float

    poll_interval_s: float
    rule_check_interval_s: float
    data_dir: Path
    heartbeat_path: Path
    webhook_url: str
    daily_summary_hour_utc: int
    exchange_origin: str
    rpc_url: str
    build_id: str

    max_leverage: int = DEFAULT_MAX_LEVERAGE
    close_mode: str = "fixed"
    vol_window_h: float = 6.0
    vol_candle_interval: str = "15m"
    vol_multiplier: float = 2.0
    close_min: float = 0.003
    close_max: float = 0.0075
    vol_leverage_buffer: float = 0.003
    leverage_derate_step: float = 0.001
    leverage_derate_max_steps: int = 2
    close_overshoot_alert_fraction: float = 0.5
    pace_schedule: tuple[Checkpoint, ...] = ()
    pace_start: float = 0.0

    @property
    def mode(self) -> str:
        return "LIVE" if self.live else "DRY_RUN"

    @property
    def liquidation_move(self) -> float:
        return liquidation_move(self.leverage)

    def redacted(self) -> dict:
        out = {}
        for name in self.__dataclass_fields__:
            value = getattr(self, name)
            if name in ("session_key", "stake_wallet_key"):
                value = "<set>" if value else "<unset>"
            elif name == "wallet_address" and value:
                value = value[:6] + "…" + value[-4:]
            elif name == "webhook_url" and value:
                value = "<set>"
            elif isinstance(value, Path):
                value = str(value)
            out[name] = value
        return out


def _resolve_live(env: dict[str, str]) -> bool:
    dry_run = _bool(env, "DRY_RUN", True)
    live = _bool(env, "LIVE", False)
    confirm = _str(env, "LIVE_CONFIRM")
    if dry_run:
        if live:
            raise ConfigError("LIVE=true conflicts with DRY_RUN=true; set DRY_RUN=false to trade live")
        return False
    if not live:
        raise ConfigError("DRY_RUN=false requires LIVE=true (and LIVE_CONFIRM) to trade live")
    if confirm != LIVE_CONFIRM_PHRASE:
        raise ConfigError(f"live trading requires LIVE_CONFIRM={LIVE_CONFIRM_PHRASE}")
    return True


def load_config(env: dict[str, str] | None = None, dotenv_path: str | os.PathLike = ".env") -> Config:
    if env is None:
        env = {**load_dotenv(dotenv_path), **os.environ}
    live = _resolve_live(env)

    markets = ["BTC"]
    if _bool(env, "ENABLE_ETH", False):
        markets.append("ETH")

    max_leverage = _int(env, "MAX_LEVERAGE", DEFAULT_MAX_LEVERAGE)
    if not 1 <= max_leverage <= ABSOLUTE_MAX_LEVERAGE:
        raise ConfigError(f"MAX_LEVERAGE must be between 1 and {ABSOLUTE_MAX_LEVERAGE}, got {max_leverage}")
    close_mode = _str(env, "CLOSE_MODE", "fixed").lower()
    if close_mode not in CLOSE_MODES:
        raise ConfigError(f"CLOSE_MODE must be one of {CLOSE_MODES}")

    leverage = _int(env, "LEVERAGE", 40)
    tp = _float(env, "TAKE_PROFIT_MOVE_PCT", 1.0) / 100.0
    tp_fraction = _float(env, "TP_MAX_FRACTION_OF_LIQ", 0.60)
    if not 0 < tp_fraction <= 0.8:
        raise ConfigError("TP_MAX_FRACTION_OF_LIQ must be in (0, 0.8]")
    emergency = _float(env, "EMERGENCY_BUST_DISTANCE_PCT", 0.30) / 100.0
    if close_mode == "fixed":
        if not 1 <= leverage <= max_leverage:
            raise ConfigError(f"LEVERAGE must be between 1 and MAX_LEVERAGE={max_leverage}, got {leverage}")
        liq = liquidation_move(leverage)
        if tp <= 0:
            raise ConfigError("TAKE_PROFIT_MOVE_PCT must be positive")
        if tp > tp_fraction * liq:
            raise ConfigError(
                f"TAKE_PROFIT_MOVE_PCT={tp * 100:.3f}% is {tp / liq:.0%} of the {liq * 100:.3f}% liquidation "
                f"distance at {leverage}x; the limit is {tp_fraction:.0%} "
                f"(max {tp_fraction * liq * 100:.3f}%). Lower the take-profit or the leverage."
            )
        if emergency >= liq - tp:
            raise ConfigError("EMERGENCY_BUST_DISTANCE_PCT must be smaller than the gap between take-profit and liquidation")

    vol_window_h = _float(env, "VOL_WINDOW_H", 6.0)
    vol_interval = _str(env, "VOL_CANDLE_INTERVAL", "15m")
    vol_multiplier = _float(env, "VOL_MULTIPLIER", 2.0)
    close_min = _float(env, "CLOSE_MIN_PCT", 0.30) / 100.0
    close_max = _float(env, "CLOSE_MAX_PCT", 0.75) / 100.0
    vol_buffer = _float(env, "VOL_LEVERAGE_BUFFER_PCT", 0.30) / 100.0
    derate_step = _float(env, "LEVERAGE_DERATE_STEP_PCT", 0.10) / 100.0
    derate_max = _int(env, "LEVERAGE_DERATE_MAX_STEPS", 2)
    if vol_interval not in CANDLE_INTERVALS:
        raise ConfigError(f"VOL_CANDLE_INTERVAL must be one of {CANDLE_INTERVALS}")
    if vol_window_h < 1 or vol_multiplier <= 0:
        raise ConfigError("VOL_WINDOW_H must be >= 1 and VOL_MULTIPLIER positive")
    if not 0.001 <= close_min <= close_max <= 0.02:
        raise ConfigError("need 0.1 <= CLOSE_MIN_PCT <= CLOSE_MAX_PCT <= 2")
    if vol_buffer - DEFAULT_BUST_BUFFER < MIN_TP_LIQ_GAP:
        raise ConfigError(f"VOL_LEVERAGE_BUFFER_PCT must leave at least {MIN_TP_LIQ_GAP:.2%} between take-profit "
                          f"and liquidation after the {DEFAULT_BUST_BUFFER:.3%} bust buffer")
    if derate_step < 0 or derate_max < 0:
        raise ConfigError("LEVERAGE_DERATE_STEP_PCT and LEVERAGE_DERATE_MAX_STEPS must not be negative")
    try:
        pace_schedule = parse_schedule(_str(env, "PACE_SCHEDULE"))
        pace_start = parse_utc(_str(env, "PACE_START"), "PACE_START") if _str(env, "PACE_START") else 0.0
    except PaceError as exc:
        raise ConfigError(str(exc)) from exc

    margin = _float(env, "MARGIN_PER_LEG_USD", 250.0)
    if margin < 10:
        raise ConfigError("MARGIN_PER_LEG_USD must be at least the protocol minimum of 10")

    wallet = _str(env, "WALLET_ADDRESS").lower()
    session_key = _str(env, "SESSION_KEY")
    stake_key = _str(env, "STAKE_WALLET_PRIVATE_KEY")
    stake_mode = _str(env, "STAKE_MODE", "remind").lower()
    if stake_mode not in STAKE_MODES:
        raise ConfigError(f"STAKE_MODE must be one of {STAKE_MODES}")
    if wallet and not _ADDRESS_RE.match(wallet):
        raise ConfigError("WALLET_ADDRESS is not a 0x address")
    if session_key and not _KEY_RE.match(session_key):
        raise ConfigError("SESSION_KEY must be a 32-byte hex private key")
    if stake_key and not _KEY_RE.match(stake_key):
        raise ConfigError("STAKE_WALLET_PRIVATE_KEY must be a 32-byte hex private key")
    if live:
        if not wallet:
            raise ConfigError("live trading requires WALLET_ADDRESS")
        if not session_key:
            raise ConfigError("live trading requires SESSION_KEY")
    if stake_mode == "auto" and live and not stake_key:
        raise ConfigError("STAKE_MODE=auto requires STAKE_WALLET_PRIVATE_KEY in live mode")
    if stake_mode != "auto" and stake_key:
        raise ConfigError("STAKE_WALLET_PRIVATE_KEY is set but STAKE_MODE is not auto; remove the key")

    unknown_policy = _str(env, "UNKNOWN_POSITION_POLICY", "close").lower()
    if unknown_policy not in UNKNOWN_POSITION_POLICIES:
        raise ConfigError(f"UNKNOWN_POSITION_POLICY must be one of {UNKNOWN_POSITION_POLICIES}")

    daily_cap = _float(env, "DAILY_LOSS_CAP_USD", 100.0)
    if daily_cap <= 0:
        raise ConfigError("DAILY_LOSS_CAP_USD must be positive; it is the primary stop and cannot be disabled")

    leg_timeout = _float(env, "LEG_FILL_TIMEOUT_S", 1200)
    if leg_timeout >= 3600:
        raise ConfigError("LEG_FILL_TIMEOUT_S must be below the 1h intent lifetime")

    try:
        tranches = load_tranches(_str(env, "BUDGET_TRANCHES"), _str(env, "BUDGET_TRANCHES_FILE"))
    except TrancheError as exc:
        raise ConfigError(str(exc)) from exc

    latency_count = _int(env, "CLOSE_LATENCY_ALERT_COUNT", 2)
    if latency_count < 1:
        raise ConfigError("CLOSE_LATENCY_ALERT_COUNT must be at least 1")

    data_dir = Path(_str(env, "DATA_DIR", "./data"))
    return Config(
        live=live,
        wallet_address=wallet,
        session_key=session_key,
        stake_wallet_key=stake_key,
        markets=tuple(markets),
        margin_per_leg_usd=margin,
        size_from_balance=_bool(env, "SIZE_FROM_BALANCE", False),
        balance_buffer_usd=_float(env, "BALANCE_BUFFER_USD", 50.0),
        leverage=leverage,
        take_profit_move=tp,
        tp_max_fraction_of_liq=tp_fraction,
        emergency_bust_distance=emergency,
        max_cost_per_paper=_optional_limit(env, "MAX_COST_PER_PAPER", 0.0016),
        daily_loss_cap_usd=daily_cap,
        round_cost_safety_factor=_float(env, "ROUND_COST_SAFETY_FACTOR", 2.0),
        round_cooldown_s=_float(env, "ROUND_COOLDOWN_S", 60),
        leg_fill_timeout_s=leg_timeout,
        open_abandon_timeout_s=_float(env, "OPEN_ABANDON_TIMEOUT_S", 1800),
        intent_grace_s=_float(env, "INTENT_GRACE_S", 300),
        close_resign_before_expiry_s=_float(env, "CLOSE_RESIGN_BEFORE_EXPIRY_S", 600),
        settle_grace_s=_float(env, "SETTLE_GRACE_S", 600),
        relayer_down_halt_s=_float(env, "RELAYER_DOWN_HALT_S", 1800),
        relayer_max_consecutive_failures=_int(env, "RELAYER_MAX_CONSECUTIVE_FAILURES", 5),
        btc_daily_vol=_float(env, "BTC_DAILY_VOL_PCT", 2.5) / 100.0,
        eth_daily_vol=_float(env, "ETH_DAILY_VOL_PCT", 3.5) / 100.0,
        expected_fill_latency_h=_float(env, "EXPECTED_FILL_LATENCY_H", 0.25),
        tail_growth_usd_per_h=_float(env, "TAIL_GROWTH_USD_PER_H", 2_170_000),
        min_fee_yield_per_paper_day=_optional_limit(env, "MIN_FEE_YIELD_PER_PAPER_DAY", None),
        max_payback_days=_optional_limit(env, "MAX_PAYBACK_DAYS", None),
        min_lp_fraction_of_cap=_optional_limit(env, "MIN_LP_FRACTION_OF_CAP", None),
        require_lp_trend_nonnegative=_bool(env, "REQUIRE_LP_TREND_NONNEGATIVE", False),
        tranches=tranches,
        min_mint_rate=_float(env, "MIN_MINT_RATE", 28.0),
        opportunistic_lp_min_usd=_float(env, "OPPORTUNISTIC_LP_MIN_USD", 500_000),
        opportunistic_margin_per_leg_usd=_float(env, "OPPORTUNISTIC_MARGIN_PER_LEG_USD", 0.0),
        lp_floor_usd=_optional_limit(env, "LP_FLOOR_USD", 500_000),
        lp_trend_horizon_h=_float(env, "LP_TREND_HORIZON_H", 2.0),
        mint_rate_stop=_optional_limit(env, "MINT_RATE_STOP", 20.0),
        close_latency_alert_s=_float(env, "CLOSE_LATENCY_ALERT_S", 300),
        close_latency_alert_count=latency_count,
        close_latency_pause_s=_float(env, "CLOSE_LATENCY_PAUSE_S", 1800),
        stake_mode=stake_mode,
        stake_min_paper=_float(env, "STAKE_MIN_PAPER", 1000),
        unknown_position_policy=unknown_policy,
        flatten_hedged_on_shutdown=_bool(env, "FLATTEN_HEDGED_ON_SHUTDOWN", False),
        shutdown_grace_s=_float(env, "SHUTDOWN_GRACE_S", 600),
        dry_run_force_rounds=_bool(env, "DRY_RUN_FORCE_ROUNDS", False) and not live,
        sim_fill_delay_s=_float(env, "SIM_FILL_DELAY_S", 30),
        sim_start_balance_usd=_float(env, "SIM_START_BALANCE_USD", 1000),
        poll_interval_s=_float(env, "POLL_INTERVAL_S", 10),
        rule_check_interval_s=_float(env, "RULE_CHECK_INTERVAL_S", 900),
        data_dir=data_dir,
        heartbeat_path=Path(_str(env, "HEARTBEAT_PATH", str(data_dir / "heartbeat.json"))),
        webhook_url=_str(env, "WEBHOOK_URL"),
        daily_summary_hour_utc=_int(env, "DAILY_SUMMARY_HOUR_UTC", 0),
        exchange_origin=_str(env, "EXCHANGE_ORIGIN", "https://exchange.papertrade.xyz").rstrip("/"),
        rpc_url=_str(env, "RPC_URL", "https://rpc.hyperliquid.xyz/evm"),
        build_id=_str(env, "PAPERTRADE_BUILD_ID"),
        max_leverage=max_leverage,
        close_mode=close_mode,
        vol_window_h=vol_window_h,
        vol_candle_interval=vol_interval,
        vol_multiplier=vol_multiplier,
        close_min=close_min,
        close_max=close_max,
        vol_leverage_buffer=vol_buffer,
        leverage_derate_step=derate_step,
        leverage_derate_max_steps=derate_max,
        close_overshoot_alert_fraction=_float(env, "CLOSE_OVERSHOOT_ALERT_FRACTION", 0.5),
        pace_schedule=pace_schedule,
        pace_start=pace_start,
    )
