"""Typed views of the public feeds and the account stream.

Parsers here are deliberately strict: an unexpected shape raises
`FeedFormatError`, which the engine treats as "source of truth unavailable"
(no new rounds) rather than guessing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .protocol import WAD


class FeedFormatError(ValueError):
    pass


def _int(value: Any, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FeedFormatError(f"{name}: expected integer, got {value!r}") from exc


def wad_to_float(raw: int) -> float:
    return raw / WAD


@dataclass(frozen=True)
class MintParams:
    flat_rate: float
    decay_scale_usd: float
    cliff_usd: float


@dataclass(frozen=True)
class ProtocolSummary:
    block: int
    at_ms: int
    tvl_usd: float
    lp_usd: float
    queue_usd: float
    supply_paper: float
    staked_paper: float
    tracked_lp_usd: float
    tail_progress_usd: float
    staker_cap_usd: float
    excess_usd: float
    fees_pending_usd: float
    fees_lifetime_usd: float
    mint: MintParams

    @classmethod
    def parse(cls, data: dict) -> "ProtocolSummary":
        try:
            balances, paper = data["balances"], data["paper"]
            return cls(
                block=_int(data["source"]["block"], "block"),
                at_ms=_int(data["source"]["at"], "at"),
                tvl_usd=wad_to_float(_int(balances["tvl"], "tvl")),
                lp_usd=wad_to_float(_int(balances["lp"], "lp")),
                queue_usd=wad_to_float(_int(balances["queue"], "queue")),
                supply_paper=wad_to_float(_int(paper["supply"], "supply")),
                staked_paper=wad_to_float(_int(paper["staked"], "staked")),
                tracked_lp_usd=wad_to_float(_int(paper["trackedLp"], "trackedLp")),
                tail_progress_usd=wad_to_float(_int(paper["tailProgress"], "tailProgress")),
                staker_cap_usd=wad_to_float(_int(paper["cap"], "cap")),
                excess_usd=wad_to_float(_int(paper["excess"], "excess")),
                fees_pending_usd=wad_to_float(_int(data["fees"]["pending"], "fees.pending")),
                fees_lifetime_usd=wad_to_float(_int(data["fees"]["lifetime"], "fees.lifetime")),
                mint=MintParams(
                    flat_rate=wad_to_float(_int(paper["rate"], "rate")),
                    decay_scale_usd=wad_to_float(_int(paper["decay"], "decay")),
                    cliff_usd=wad_to_float(_int(paper["cliff"], "cliff")),
                ),
            )
        except KeyError as exc:
            raise FeedFormatError(f"protocol summary missing {exc}") from exc


@dataclass(frozen=True)
class QueueSummary:
    total_usd: float
    count: int

    @property
    def is_open(self) -> bool:
        return self.count > 0 or self.total_usd > 0

    @classmethod
    def parse(cls, data: dict) -> "QueueSummary":
        try:
            return cls(total_usd=wad_to_float(_int(data["total"], "total")), count=_int(data["count"], "count"))
        except KeyError as exc:
            raise FeedFormatError(f"queue summary missing {exc}") from exc


@dataclass(frozen=True)
class Instrument:
    id: int
    symbol: str
    price_scale: int
    active: bool
    paused: bool
    retired: bool
    close_only: bool
    rate_multiplier: float
    position_multiplier: float
    max_leverage: int
    max_position_usd: float
    bust_buffer: float
    base_rate: float
    reference_notional_usd: float
    long_oi: float
    short_oi: float
    long_cap: float
    short_cap: float

    @property
    def openable(self) -> bool:
        return self.active and not self.paused and not self.retired and not self.close_only

    @classmethod
    def parse(cls, row: list) -> "Instrument":
        if not isinstance(row, list) or len(row) != 23:
            raise FeedFormatError(f"instrument tuple has unexpected shape: {row!r:.200}")
        return cls(
            id=_int(row[0], "id"),
            symbol=str(row[1]),
            price_scale=_int(row[3], "priceScale"),
            active=bool(row[4]),
            paused=bool(row[5]),
            retired=bool(row[6]),
            close_only=bool(row[7]),
            rate_multiplier=wad_to_float(_int(row[12], "rateMultiplier")),
            position_multiplier=wad_to_float(_int(row[13], "positionMultiplier")),
            max_leverage=_int(row[14], "maxLeverage"),
            max_position_usd=wad_to_float(_int(row[15], "maxPosition")),
            bust_buffer=wad_to_float(_int(row[16], "bustBuffer")),
            base_rate=wad_to_float(_int(row[17], "baseRate")),
            reference_notional_usd=wad_to_float(_int(row[18], "referenceNotional")),
            long_oi=wad_to_float(_int(row[19], "longOi")),
            short_oi=wad_to_float(_int(row[20], "shortOi")),
            long_cap=wad_to_float(_int(row[21], "longCap")),
            short_cap=wad_to_float(_int(row[22], "shortCap")),
        )


@dataclass(frozen=True)
class TradingState:
    block: int
    trading_paused: bool
    min_margin_usd: float
    min_notional_usd: float
    instruments: dict[str, Instrument]
    paused_actions: frozenset[str]
    paper_stake_minimum: float
    acc_reward_per_share_raw: int = 0

    @classmethod
    def parse(cls, data: dict) -> "TradingState":
        try:
            if data.get("schemaVersion") != 1:
                raise FeedFormatError(f"unexpected /state/trading schemaVersion {data.get('schemaVersion')!r}")
            instruments = {inst.symbol: inst for inst in (Instrument.parse(r) for r in data["instruments"])}
            intake = data.get("intake") or [0, "0", "0", []]
            return cls(
                block=_int(data["indexedThroughBlock"], "block"),
                trading_paused=bool(data["tradingPaused"]),
                min_margin_usd=wad_to_float(_int(data["minimumTradeSizeRaw"], "minimumTradeSize")),
                min_notional_usd=wad_to_float(_int(data["minimumOpenNotionalRaw"], "minimumOpenNotional")),
                instruments=instruments,
                paused_actions=frozenset(intake[3]),
                paper_stake_minimum=wad_to_float(_int(intake[1], "paperStakeMinimum")),
                acc_reward_per_share_raw=_int(data.get("accRewardPerShareRaw", 0), "accRewardPerShare"),
            )
        except (KeyError, IndexError) as exc:
            raise FeedFormatError(f"/state/trading missing {exc}") from exc


@dataclass(frozen=True)
class Position:
    position_id: int
    instrument_id: int
    is_long: bool
    use_debt: bool
    entry_price: float
    margin_wad: int
    leverage: int
    bust_price: float
    opened_at_ms: int
    intent_id: str | None

    @property
    def margin_usd(self) -> float:
        return wad_to_float(self.margin_wad)


@dataclass(frozen=True)
class Operation:
    """An intent the relayer has accepted and not yet settled."""

    intent_id: str
    action: str
    accepted_at_ms: int
    deadline: int
    close_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AccountSnapshot:
    address: str
    indexed_block: int
    indexed_timestamp: int
    available_balance_usd: float
    queued_usd: float
    paper_balance: float
    paper_staked: float
    session_keys: dict[str, int]
    positions: tuple[Position, ...]
    operations: tuple[Operation, ...]
    paper_staked_wad: int = 0
    paper_pending_base_wad: int = 0
    paper_reward_debt_wad: int = 0

    def claimable_rewards_usd(self, acc_reward_per_share_raw: int) -> float:
        """Distributed-but-unclaimed staking rewards (MasterChef-style accounting, 1e27 accumulator)."""
        accrued = self.paper_staked_wad * acc_reward_per_share_raw // 10**27
        return wad_to_float(max(accrued - self.paper_reward_debt_wad, 0) + self.paper_pending_base_wad)

    def position_by_intent(self, intent_id: str) -> Position | None:
        for pos in self.positions:
            if pos.intent_id == intent_id:
                return pos
        return None

    def position(self, position_id: int) -> Position | None:
        for pos in self.positions:
            if pos.position_id == position_id:
                return pos
        return None

    def operation(self, intent_id: str) -> Operation | None:
        for op in self.operations:
            if op.intent_id == intent_id:
                return op
        return None

    @classmethod
    def parse(cls, data: dict, price_scales: dict[int, int]) -> "AccountSnapshot":
        try:
            positions = []
            for row in data["openPositions"]:
                if len(row) != 10:
                    raise FeedFormatError(f"position tuple has unexpected shape: {row!r:.200}")
                instrument_id = _int(row[1], "instrumentId")
                scale = price_scales.get(instrument_id)
                if not scale:
                    raise FeedFormatError(f"no price scale for instrument {instrument_id}")
                positions.append(
                    Position(
                        position_id=_int(row[0], "positionId"),
                        instrument_id=instrument_id,
                        is_long=bool(row[2]),
                        use_debt=bool(row[3]),
                        entry_price=_int(row[4], "entry") / scale,
                        margin_wad=_int(row[5], "margin"),
                        leverage=_int(row[6], "leverage"),
                        bust_price=_int(row[7], "bust") / scale,
                        opened_at_ms=_int(row[8], "openedAt"),
                        intent_id=row[9].lower() if row[9] else None,
                    )
                )
            operations = []
            for row in data["operations"]:
                if len(row) != 10:
                    raise FeedFormatError(f"operation tuple has unexpected shape: {row!r:.200}")
                operations.append(
                    Operation(
                        intent_id=str(row[0]).lower(),
                        action=str(row[1]),
                        accepted_at_ms=_int(row[7], "acceptedAt"),
                        deadline=_int(row[8], "deadline"),
                        close_ids=tuple(_int(x, "closeId") for x in (row[9] or [])),
                    )
                )
            funds, paper = data["funds"], data["paper"]
            return cls(
                address=str(data["address"]).lower(),
                indexed_block=_int(data["indexedThroughBlock"], "indexedThroughBlock"),
                indexed_timestamp=_int(data["indexedThroughTimestamp"], "indexedThroughTimestamp"),
                available_balance_usd=wad_to_float(_int(funds["availableBalance"], "availableBalance")),
                queued_usd=wad_to_float(_int(data["queued"], "queued")),
                paper_balance=wad_to_float(_int(paper["balance"], "paper.balance")),
                paper_staked=wad_to_float(_int(paper["staked"], "paper.staked")),
                session_keys={str(k).lower(): _int(v, "sessionExpiry") for k, v in data["sessionKeys"]},
                positions=tuple(positions),
                operations=tuple(operations),
                paper_staked_wad=_int(paper["staked"], "paper.staked"),
                paper_pending_base_wad=_int(paper["pendingBase"], "paper.pendingBase"),
                paper_reward_debt_wad=_int(paper["rewardDebt"], "paper.rewardDebt"),
            )
        except KeyError as exc:
            raise FeedFormatError(f"wallet snapshot missing {exc}") from exc


class CloseKind(str, Enum):
    CLOSED = "PositionClosed"
    LIQUIDATED = "Liquidated"


@dataclass(frozen=True)
class ClosedTrade:
    position_id: int
    instrument_id: int
    is_long: bool
    kind: CloseKind
    entry_price: float
    exit_price: float
    margin_usd: float
    leverage: int
    pnl_usd: float
    fee_usd: float
    paper_minted: float
    paper_mint_basis_usd: float
    timestamp_ms: int

    @property
    def liquidated(self) -> bool:
        return self.kind is CloseKind.LIQUIDATED

    @property
    def net_pnl_usd(self) -> float:
        """PnL credited to the trader: adjusted PnL minus the win fee."""
        return self.pnl_usd - self.fee_usd

    @classmethod
    def parse(cls, item: dict) -> "ClosedTrade":
        try:
            scale = _int(item["priceScaleRaw"], "priceScale")
            return cls(
                position_id=_int(item["positionId"], "positionId"),
                instrument_id=_int(item["instrumentId"], "instrumentId"),
                is_long=bool(item["isLong"]),
                kind=CloseKind(item["kind"]),
                entry_price=_int(item["entryPriceRaw"], "entry") / scale,
                exit_price=_int(item["exitPriceRaw"], "exit") / scale,
                margin_usd=wad_to_float(_int(item["marginRaw"], "margin")),
                leverage=_int(item["leverage"], "leverage"),
                pnl_usd=wad_to_float(_int(item["adjustedPnlRaw"], "adjustedPnl")),
                fee_usd=wad_to_float(_int(item["userPaidFeeRaw"], "userPaidFee")),
                paper_minted=wad_to_float(_int(item["paperMintedRaw"], "paperMinted")),
                paper_mint_basis_usd=wad_to_float(_int(item["paperMintBasisRaw"], "paperMintBasis")),
                timestamp_ms=_int(item["timestampMs"], "timestampMs"),
            )
        except (KeyError, ValueError) as exc:
            raise FeedFormatError(f"trade history item malformed: {exc}") from exc


@dataclass(frozen=True)
class ProtocolHistory:
    """Hourly cumulative series from /query/protocol/history?interval=1h."""

    start_ms: int
    interval_ms: int
    columns: dict[str, list[float]] = field(default_factory=dict)

    def series(self, name: str) -> list[float]:
        return self.columns.get(name, [])

    @classmethod
    def parse(cls, data: dict) -> "ProtocolHistory":
        try:
            return cls(
                start_ms=_int(data["startMs"], "startMs"),
                interval_ms=_int(data["intervalMs"], "intervalMs"),
                columns={k: [float(x) for x in v] for k, v in data["columns"].items()},
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedFormatError(f"protocol history malformed: {exc}") from exc
