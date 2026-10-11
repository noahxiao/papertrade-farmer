from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account

from ptfarmer.config import LIVE_CONFIRM_PHRASE, ConfigError, load_config, load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def cfg(**env):
    return load_config(env={k: str(v) for k, v in env.items()})


def live_env(**extra):
    session = Account.create()
    wallet = Account.create()
    return {"DRY_RUN": "false", "LIVE": "true", "LIVE_CONFIRM": LIVE_CONFIRM_PHRASE,
            "WALLET_ADDRESS": wallet.address, "SESSION_KEY": session.key.hex(), **extra}


def test_defaults_are_safe():
    c = cfg()
    assert not c.live and c.mode == "DRY_RUN"
    assert c.markets == ("BTC",)
    assert (c.leverage, c.take_profit_move) == (40, 0.01)
    assert c.max_cost_per_paper == 0.0016
    assert c.stake_mode == "remind"


@pytest.mark.parametrize("env", [
    {"LIVE": "true"},
    {"DRY_RUN": "false"},
    {"DRY_RUN": "false", "LIVE": "true"},
    {"DRY_RUN": "false", "LIVE": "true", "LIVE_CONFIRM": "yes"},
    {"DRY_RUN": "true", "LIVE": "true", "LIVE_CONFIRM": LIVE_CONFIRM_PHRASE},
])
def test_live_needs_every_flag(env):
    with pytest.raises(ConfigError):
        cfg(**env)


def test_live_with_all_flags_and_keys():
    c = load_config(env=live_env())
    assert c.live and c.mode == "LIVE"
    assert c.redacted()["session_key"] != c.session_key
    assert c.redacted()["wallet_address"] != c.wallet_address


def test_live_requires_wallet_and_session_key():
    env = live_env()
    del env["SESSION_KEY"]
    with pytest.raises(ConfigError, match="SESSION_KEY"):
        load_config(env=env)


def test_dry_run_force_never_applies_live():
    assert not load_config(env=live_env(DRY_RUN_FORCE_ROUNDS="true")).dry_run_force_rounds


def test_leverage_hard_cap():
    assert cfg(LEVERAGE=100, TAKE_PROFIT_MOVE_PCT=0.3).leverage == 100
    with pytest.raises(ConfigError, match="LEVERAGE"):
        cfg(LEVERAGE=101, TAKE_PROFIT_MOVE_PCT=0.3)


def test_take_profit_must_stay_within_60pct_of_liquidation():
    cfg(LEVERAGE=40, TAKE_PROFIT_MOVE_PCT=1.0)  # 41%
    with pytest.raises(ConfigError, match="liquidation"):
        cfg(LEVERAGE=40, TAKE_PROFIT_MOVE_PCT=1.5)  # 61%
    with pytest.raises(ConfigError, match="liquidation"):
        cfg(LEVERAGE=80, TAKE_PROFIT_MOVE_PCT=0.75)  # 62%


def test_cost_limit_can_be_disabled_but_daily_cap_cannot():
    assert cfg(MAX_COST_PER_PAPER="off").max_cost_per_paper is None
    with pytest.raises(ConfigError, match="DAILY_LOSS_CAP_USD"):
        cfg(DAILY_LOSS_CAP_USD=0)


def test_stake_key_rules():
    key = Account.create().key.hex()
    with pytest.raises(ConfigError, match="STAKE_MODE"):
        cfg(STAKE_WALLET_PRIVATE_KEY=key)
    with pytest.raises(ConfigError, match="STAKE_WALLET_PRIVATE_KEY"):
        load_config(env=live_env(STAKE_MODE="auto"))
    assert cfg(STAKE_MODE="auto").stake_mode == "auto", "dry run auto-stakes the simulated balance"


def test_tranche_schedule_validation():
    assert cfg(BUDGET_TRANCHES='[{"name": "a", "budget_usd": 5}]').tranches[0].budget_usd == 5
    for bad in ('{"name": "a"}', '[{"name": "a", "budget_usd": -1}]', '[{"name": "a", "budget_usd": 1, "x": 1}]',
                '[{"name": "a", "budget_usd": 1}, {"name": "a", "budget_usd": 1}]',
                '[{"name": "a", "budget_usd": 1, "available_until": "tomorrow"}]', "not json"):
        with pytest.raises(ConfigError):
            cfg(BUDGET_TRANCHES=bad)


def test_leg_timeout_below_intent_lifetime():
    with pytest.raises(ConfigError):
        cfg(LEG_FILL_TIMEOUT_S=3600)


@pytest.mark.parametrize("path", [".env.example", "profiles/max-paper.env"])
def test_shipped_env_files_are_valid_and_dry_run(path, monkeypatch):
    monkeypatch.chdir(ROOT)
    env = load_dotenv(ROOT / path)
    c = load_config(env=env)
    assert not c.live, "shipped files must never enable live trading"
    assert not any(v for k, v in env.items() if k in ("SESSION_KEY", "STAKE_WALLET_PRIVATE_KEY", "WALLET_ADDRESS"))


def test_max_paper_profile_matches_scenario(monkeypatch):
    monkeypatch.chdir(ROOT)
    c = load_config(env=load_dotenv(ROOT / "profiles" / "max-paper.env"))
    assert (c.leverage, c.take_profit_move, c.markets) == (40, 0.01, ("BTC",))
    assert c.size_from_balance and 2500 <= c.margin_per_leg_usd <= 3000
    assert c.max_cost_per_paper is None and c.daily_loss_cap_usd == 1000
    assert c.round_cooldown_s == 0 and c.stake_mode == "auto"
    assert [t.name for t in c.tranches] and any(t.kind == "opportunistic" for t in c.tranches)
