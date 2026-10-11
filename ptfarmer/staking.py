"""Staking modules (STAKE_MODE=off|remind|auto).

Staking needs the *wallet* signature, not the session key. `AutoStaker`
therefore holds the wallet private key, which can also sign withdrawals:
it is a custody decision, off by default, and documented as such.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Callable, Protocol

from .alerts import Notifier
from .models import AccountSnapshot
from .protocol import CONTRACTS, INTENT_LIFETIME_S, WAD
from .relayer import RelayerClient, SubmitStatus
from .signing import IntentSigner
from .store import Store

log = logging.getLogger(__name__)


class StakingModule(Protocol):
    mode: str

    def after_round(self, account: AccountSnapshot, stake_minimum: float) -> float:
        """Called after each settled round (and periodically). Returns PAPER submitted for staking."""
        ...


def stakeable_amount(account: AccountSnapshot, configured_min: float, protocol_min: float) -> float:
    if any(op.action == "paperStake" for op in account.operations):
        return 0.0
    amount = account.paper_balance
    return amount if amount >= max(configured_min, protocol_min) else 0.0


class NoStaking:
    mode = "off"

    def after_round(self, account: AccountSnapshot, stake_minimum: float) -> float:
        return 0.0


class StakeReminder:
    mode = "remind"

    def __init__(self, notifier: Notifier, min_paper: float, cooldown_s: float = 6 * 3600):
        self.notifier = notifier
        self.min_paper = min_paper
        self.cooldown_s = cooldown_s

    def after_round(self, account: AccountSnapshot, stake_minimum: float) -> float:
        amount = stakeable_amount(account, self.min_paper, stake_minimum)
        if amount:
            self.notifier.alert("stake_reminder", f"{amount:,.0f} PAPER unstaked; stake it from the app (wallet signature required)",
                                level="info", cooldown_s=self.cooldown_s)
        return 0.0


class AutoStaker:
    mode = "auto"

    def __init__(self, wallet_signer: IntentSigner, relayer: RelayerClient, store: Store, notifier: Notifier,
                 min_paper: float, clock: Callable[[], float] = time.time):
        if wallet_signer.address != wallet_signer.user:
            raise ValueError("STAKE_WALLET_PRIVATE_KEY does not belong to WALLET_ADDRESS")
        self.signer = wallet_signer
        self.relayer = relayer
        self.store = store
        self.notifier = notifier
        self.min_paper = min_paper
        self.clock = clock
        self._scope = f"paper:{CONTRACTS['paperStaking']}:{wallet_signer.user}"

    def after_round(self, account: AccountSnapshot, stake_minimum: float) -> float:
        amount = stakeable_amount(account, self.min_paper, stake_minimum)
        if not amount:
            return 0.0
        amount_wad = int(amount * WAD) // 10**12 * 10**12
        nonce = self.store.next_nonce(self._scope, (secrets.randbelow(2**24 - 1) + 1) * 256)
        si = self.signer.stake(amount_wad, nonce, int(self.clock()) + INTENT_LIFETIME_S)
        self.store.record_intent(si.intent_id, None, "stake", "paperStake", nonce, int(self.clock()) + INTENT_LIFETIME_S, si.signed)
        result = self.relayer.submit("paperStake", si.signed, si.intent_id)
        self.store.set_intent_status(result.intent_id, result.status.value, result.code)
        staked = amount_wad / WAD
        self.store.record_stake(staked, result.status.value, result.intent_id, result.code or "")
        if result.status is SubmitStatus.ACCEPTED:
            log.info("auto-stake submitted: %.0f PAPER (%s)", staked, result.intent_id)
            return staked
        self.notifier.alert("stake_failed", f"auto-stake of {staked:,.0f} PAPER {result.status.value}: {result.code or result.reason}")
        return 0.0


class SimAutoStaker:
    """DRY_RUN counterpart of AutoStaker: moves simulated PAPER to staked."""

    mode = "auto"

    def __init__(self, sim_venue, store: Store, min_paper: float):
        self.sim = sim_venue
        self.store = store
        self.min_paper = min_paper

    def after_round(self, account: AccountSnapshot, stake_minimum: float) -> float:
        amount = stakeable_amount(account, self.min_paper, stake_minimum)
        if amount:
            self.sim.stake_all(amount)
            self.store.record_stake(amount, "accepted", None, "simulated")
        return amount
