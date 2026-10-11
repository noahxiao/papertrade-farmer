"""Composition root: turn a validated Config into wired-up components."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from .alerts import Notifier
from .config import Config
from .context import MarketView
from .engine import Farmer
from .feeds import PublicFeeds
from .guards import load_baseline
from .protocol import CONTRACTS
from .relayer import RelayerClient
from .signing import IntentSigner
from .staking import AutoStaker, NoStaking, SimAutoStaker, StakeReminder, StakingModule
from .store import Store
from .venue import LiveVenue, SimVenue, Venue


@dataclass
class Components:
    cfg: Config
    store: Store
    feeds: PublicFeeds
    view: MarketView
    notifier: Notifier
    baseline: dict
    venue: Venue | None = None
    farmer: Farmer | None = None


def build_readonly(cfg: Config, clock: Callable[[], float] = time.time) -> Components:
    store = Store(cfg.data_dir, clock)
    feeds = PublicFeeds(cfg.exchange_origin, cfg.rpc_url)
    baseline = load_baseline()
    notifier = Notifier(cfg.webhook_url, store, clock)
    view = MarketView(cfg, feeds, store, baseline, clock)
    return Components(cfg, store, feeds, view, notifier, baseline)


def _staker(cfg: Config, c: Components, relayer: RelayerClient | None, sim: SimVenue | None) -> StakingModule:
    if cfg.stake_mode == "remind":
        return StakeReminder(c.notifier, cfg.stake_min_paper)
    if cfg.stake_mode == "auto":
        if sim is not None:
            return SimAutoStaker(sim, c.store, cfg.stake_min_paper)
        wallet_signer = IntentSigner(cfg.stake_wallet_key, cfg.wallet_address, CONTRACTS["exchange"], CONTRACTS["paperStaking"])
        return AutoStaker(wallet_signer, relayer, c.store, c.notifier, cfg.stake_min_paper)
    return NoStaking()


def build_farmer(cfg: Config, clock: Callable[[], float] = time.time) -> Components:
    c = build_readonly(cfg, clock)
    if cfg.live:
        relayer = RelayerClient(cfg.exchange_origin, cfg.build_id)
        signer = IntentSigner(cfg.session_key, cfg.wallet_address, CONTRACTS["exchange"], CONTRACTS["paperStaking"])
        venue: Venue = LiveVenue(c.feeds, relayer, signer, c.store, clock)
        staker = _staker(cfg, c, relayer, None)
        session_address = signer.address

        def on_build_id(build_id: str) -> None:
            relayer.build_id = cfg.build_id or build_id
    else:
        view = c.view
        sim = SimVenue(
            c.store,
            mids=lambda: view.last.mids if view.last else c.feeds.hl_mids(),
            mint_rate=lambda: view.last.spot_mint_rate if view.last else 0.0,
            clock=clock,
            fill_delay_s=cfg.sim_fill_delay_s,
            start_balance_usd=cfg.sim_start_balance_usd,
        )
        venue, staker, session_address, on_build_id = sim, _staker(cfg, c, None, sim), None, None
    c.venue = venue
    c.farmer = Farmer(cfg, venue, c.view, c.store, c.notifier, c.baseline, staker=staker, rule_feeds=c.feeds,
                      session_address=session_address, clock=clock, on_build_id=on_build_id)
    return c
