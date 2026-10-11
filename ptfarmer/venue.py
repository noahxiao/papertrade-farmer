"""Trading venue port and its live / simulated implementations.

The engine only talks to `Venue`. `LiveVenue` signs with the session key and
posts to the relayer; `SimVenue` (DRY_RUN) simulates the relayer queue and
settlement against live Hyperliquid mids and never touches the relayer.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from . import economics
from .feeds import PublicFeeds
from .models import AccountSnapshot, ClosedTrade, CloseKind, Operation, Position
from .protocol import CONTRACTS, INTENT_LIFETIME_S, WAD
from .relayer import RelayerClient, SubmitResult, SubmitStatus
from .signing import IntentSigner
from .store import Store

log = logging.getLogger(__name__)

SYMBOL_BY_INSTRUMENT = {0: "BTC", 1: "ETH"}


@dataclass(frozen=True)
class PreparedIntent:
    intent_id: str
    action: str
    signed: dict
    nonce: int
    deadline: int
    position_ids: tuple[int, ...] = ()
    meta: dict = field(default_factory=dict)


class Venue(Protocol):
    def account(self) -> AccountSnapshot: ...

    def prepare_open(self, instrument_id: int, is_long: bool, margin_wad: int, leverage: int) -> PreparedIntent: ...

    def prepare_close(self, position_ids: list[int]) -> PreparedIntent: ...

    def submit(self, intent: PreparedIntent) -> SubmitResult: ...

    def cancel(self, intent_id: str) -> SubmitResult: ...

    def closed_trades(self) -> list[ClosedTrade]: ...


def _nonce_seed() -> int:
    return (secrets.randbelow(2**24 - 1) + 1) * 256


class LiveVenue:
    def __init__(self, feeds: PublicFeeds, relayer: RelayerClient, signer: IntentSigner, store: Store, clock: Callable[[], float] = time.time):
        self.feeds = feeds
        self.relayer = relayer
        self.signer = signer
        self.store = store
        self.clock = clock
        self._nonce_scope = f"session:{CONTRACTS['exchange']}:{signer.user}"

    def _deadline(self) -> int:
        return int(self.clock()) + INTENT_LIFETIME_S

    def _nonce(self) -> int:
        return self.store.next_nonce(self._nonce_scope, _nonce_seed())

    def account(self) -> AccountSnapshot:
        return self.feeds.account_snapshot(self.signer.user)

    def prepare_open(self, instrument_id: int, is_long: bool, margin_wad: int, leverage: int) -> PreparedIntent:
        nonce, deadline = self._nonce(), self._deadline()
        si = self.signer.open_position(instrument_id, is_long, margin_wad, leverage, nonce, deadline)
        return PreparedIntent(si.intent_id, "openPosition", si.signed, nonce, deadline,
                              meta={"instrument_id": instrument_id, "is_long": is_long, "margin_wad": margin_wad, "leverage": leverage})

    def prepare_close(self, position_ids: list[int]) -> PreparedIntent:
        nonce, deadline = self._nonce(), self._deadline()
        si = self.signer.close(sorted(position_ids), nonce, deadline)
        return PreparedIntent(si.intent_id, "closePositions", si.signed, nonce, deadline, tuple(sorted(position_ids)))

    def submit(self, intent: PreparedIntent) -> SubmitResult:
        return self.relayer.submit(intent.action, intent.signed, intent.intent_id)

    def cancel(self, intent_id: str) -> SubmitResult:
        si = self.signer.cancel(intent_id, self._deadline())
        return self.relayer.cancel(intent_id, si.signed)

    def closed_trades(self) -> list[ClosedTrade]:
        return self.feeds.account_trades(self.signer.user)


class SimVenue:
    """DRY_RUN venue. State is persisted in the store so restarts behave like the live venue."""

    STATE_KEY = "sim:state"

    def __init__(
        self,
        store: Store,
        mids: Callable[[], dict[str, float]],
        mint_rate: Callable[[], float],
        clock: Callable[[], float] = time.time,
        fill_delay_s: float = 30.0,
        start_balance_usd: float = 1000.0,
        bust_buffer: float = 0.000476,
        address: str = "0x" + "00" * 19 + "51",
    ):
        self.store = store
        self.mids = mids
        self.mint_rate = mint_rate
        self.clock = clock
        self.fill_delay_s = fill_delay_s
        self.bust_buffer = bust_buffer
        self.address = address
        state = store.kv_get(self.STATE_KEY)
        self.state = state or {"balance": start_balance_usd, "next_id": 1, "positions": [], "ops": [], "closed": [], "paper": 0.0, "staked": 0.0}

    def _save(self) -> None:
        self.state["closed"] = self.state["closed"][-200:]
        self.store.kv_set(self.STATE_KEY, self.state)

    def _id(self) -> str:
        return "0x" + secrets.token_hex(32)

    def _params(self, instrument_id: int) -> economics.ImpactParams:
        return economics.BTC_LAUNCH_PARAMS if instrument_id == 0 else economics.ETH_LAUNCH_PARAMS

    def _settle(self, pos: dict, mid: float, liquidated: bool) -> None:
        notional = pos["margin"] * pos["leverage"]
        signed_move = (mid - pos["entry"]) / pos["entry"] * (1 if pos["is_long"] else -1)
        rate = self.mint_rate()
        if liquidated:
            pnl, fee, basis = -pos["margin"], 0.0, pos["margin"]
        elif signed_move > 0:
            gross = notional * economics.winning_close_received(signed_move, self._params(pos["instrument_id"])) / (1 - economics.WIN_FEE)
            pnl, fee, basis = gross, gross * economics.WIN_FEE, 0.0
        else:
            pnl, fee = max(notional * signed_move, -pos["margin"]), 0.0
            basis = -pnl * (1 - economics.LOSS_CARVE)
        paper = basis * rate
        self.state["balance"] += pos["margin"] + pnl - fee
        self.state["paper"] += paper
        self.state["closed"].append({
            "position_id": pos["id"], "instrument_id": pos["instrument_id"], "is_long": pos["is_long"],
            "kind": CloseKind.LIQUIDATED.value if liquidated else CloseKind.CLOSED.value,
            "entry": pos["entry"], "exit": mid, "margin": pos["margin"], "leverage": pos["leverage"],
            "pnl": pnl, "fee": fee, "paper": paper, "basis": basis, "ts": int(self.clock() * 1000),
        })

    def _advance(self) -> None:
        now = self.clock()
        mids = self.mids()
        for pos in list(self.state["positions"]):
            mid = mids[SYMBOL_BY_INSTRUMENT[pos["instrument_id"]]]
            if (pos["is_long"] and mid <= pos["bust"]) or (not pos["is_long"] and mid >= pos["bust"]):
                self.state["positions"].remove(pos)
                self._settle(pos, mid, liquidated=True)
        for op in list(self.state["ops"]):
            if now > op["deadline"]:
                self.state["ops"].remove(op)
                continue
            if now < op["land_at"]:
                continue
            self.state["ops"].remove(op)
            if op["action"] == "openPosition":
                margin = op["margin_wad"] / WAD
                if margin > self.state["balance"]:
                    continue
                mid = mids[SYMBOL_BY_INSTRUMENT[op["instrument_id"]]]
                liq = 1.0 / op["leverage"] - self.bust_buffer
                self.state["balance"] -= margin
                self.state["positions"].append({
                    "id": self.state["next_id"], "instrument_id": op["instrument_id"], "is_long": op["is_long"],
                    "entry": mid, "margin": margin, "leverage": op["leverage"],
                    "bust": mid * (1 - liq) if op["is_long"] else mid * (1 + liq),
                    "opened_at": int(now * 1000), "intent_id": op["intent_id"],
                })
                self.state["next_id"] += 1
            elif op["action"] == "closePositions":
                for pos in [p for p in self.state["positions"] if p["id"] in op["position_ids"]]:
                    self.state["positions"].remove(pos)
                    self._settle(pos, mids[SYMBOL_BY_INSTRUMENT[pos["instrument_id"]]], liquidated=False)
        self._save()

    def account(self) -> AccountSnapshot:
        self._advance()
        positions = tuple(
            Position(p["id"], p["instrument_id"], p["is_long"], False, p["entry"], int(round(p["margin"] * WAD)),
                     p["leverage"], p["bust"], p["opened_at"], p["intent_id"])
            for p in self.state["positions"]
        )
        ops = tuple(
            Operation(o["intent_id"], o["action"], int(o["accepted_at"] * 1000), o["deadline"], tuple(o.get("position_ids", ())))
            for o in self.state["ops"]
        )
        return AccountSnapshot(self.address, 0, int(self.clock()), self.state["balance"], 0.0, self.state["paper"],
                               self.state["staked"], {}, positions, ops, paper_staked_wad=int(self.state["staked"] * WAD))

    def prepare_open(self, instrument_id: int, is_long: bool, margin_wad: int, leverage: int) -> PreparedIntent:
        deadline = int(self.clock()) + INTENT_LIFETIME_S
        return PreparedIntent(self._id(), "openPosition", {}, 0, deadline,
                              meta={"instrument_id": instrument_id, "is_long": is_long, "margin_wad": margin_wad, "leverage": leverage})

    def prepare_close(self, position_ids: list[int]) -> PreparedIntent:
        return PreparedIntent(self._id(), "closePositions", {}, 0, int(self.clock()) + INTENT_LIFETIME_S, tuple(sorted(position_ids)))

    def submit(self, intent: PreparedIntent) -> SubmitResult:
        if intent.action == "closePositions":
            pending = {pid for o in self.state["ops"] if o["action"] == "closePositions" for pid in o["position_ids"]}
            live = {p["id"] for p in self.state["positions"]}
            if not set(intent.position_ids) & live or set(intent.position_ids) & pending:
                return SubmitResult(SubmitStatus.REJECTED, intent.intent_id, code="admission_rejected", reason="noOpenPositions")
        now = self.clock()
        self.state["ops"].append({
            "intent_id": intent.intent_id, "action": intent.action, "deadline": intent.deadline,
            "accepted_at": now, "land_at": now + self.fill_delay_s,
            "position_ids": list(intent.position_ids), **intent.meta,
        })
        self._save()
        return SubmitResult(SubmitStatus.ACCEPTED, intent.intent_id)

    def cancel(self, intent_id: str) -> SubmitResult:
        for op in self.state["ops"]:
            if op["intent_id"] == intent_id:
                self.state["ops"].remove(op)
                self._save()
                return SubmitResult(SubmitStatus.ACCEPTED, intent_id)
        return SubmitResult(SubmitStatus.REJECTED, intent_id, code="intent_conflict")

    def stake_all(self, amount: float) -> None:
        amount = min(amount, self.state["paper"])
        self.state["paper"] -= amount
        self.state["staked"] += amount
        self._save()

    def closed_trades(self) -> list[ClosedTrade]:
        return [
            ClosedTrade(c["position_id"], c["instrument_id"], c["is_long"], CloseKind(c["kind"]), c["entry"], c["exit"],
                        c["margin"], c["leverage"], c["pnl"], c["fee"], c["paper"], c["basis"], c["ts"])
            for c in reversed(self.state["closed"])
        ]
