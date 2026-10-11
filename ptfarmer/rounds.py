"""Round and leg records (persisted as JSON in the `rounds` table)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum


class RoundState(str, Enum):
    OPENING = "OPENING"  # open intents submitted, waiting for both legs
    HEDGED = "HEDGED"  # both legs filled, waiting for the take-profit move
    CLOSING = "CLOSING"  # close intent(s) in flight; also used to unwind a single leg
    DONE = "DONE"  # settled; a full hedged cycle completed
    ABORTED = "ABORTED"  # settled; the pair never became hedged


class LegStatus(str, Enum):
    PENDING = "pending"  # submitted (or unconfirmed) and not yet resolved
    FILLED = "filled"
    DEAD = "dead"  # never filled: rejected, expired, cancelled or never submitted
    CLOSED = "closed"
    LIQUIDATED = "liquidated"


@dataclass
class Leg:
    side: str
    intent_id: str | None = None
    deadline: int = 0
    status: str = LegStatus.PENDING.value
    submit_status: str | None = None
    cancel_requested: bool = False
    cancel_acked_at: float | None = None
    position_id: int | None = None
    entry_price: float | None = None
    margin_usd: float | None = None
    bust_price: float | None = None
    filled_at: float | None = None
    exit_price: float | None = None
    pnl_usd: float = 0.0
    fee_usd: float = 0.0
    paper_minted: float = 0.0
    paper_basis_usd: float = 0.0

    @property
    def is_long(self) -> bool:
        return self.side == "long"

    @property
    def net_pnl_usd(self) -> float:
        return self.pnl_usd - self.fee_usd


@dataclass
class CloseRecord:
    intent_id: str
    position_ids: list[int]
    deadline: int
    submitted_at: float
    status: str  # accepted | unconfirmed | rejected | dead | superseded
    code: str | None = None


@dataclass
class Round:
    id: int
    market: str
    instrument_id: int
    state: str
    created_at: float
    margin_usd: float
    leverage: int
    tp_move: float
    long: Leg
    short: Leg
    tranche: str | None = None
    expected_cost_per_paper: float | None = None
    mint_rate_at_open: float | None = None
    close_latency_s: float | None = None
    submitted_at: float | None = None
    hedged_at: float | None = None
    entry_ref: float | None = None
    became_hedged: bool = False
    close_reason: str | None = None
    close_started_at: float | None = None
    positions_gone_at: float | None = None
    closes: list[CloseRecord] = field(default_factory=list)
    net_pnl_usd: float = 0.0
    paper_minted: float = 0.0
    cost_per_paper: float | None = None
    liquidated: bool = False
    paper_staked_after: float = 0.0
    notes: list[str] = field(default_factory=list)
    close_mode: str = "fixed"
    emergency_distance: float | None = None  # early-close distance from bust; None = config value
    liq_move: float | None = None
    sigma_hourly: float | None = None
    behind_pace: bool = False
    exit_move: float | None = None  # largest |exit fill - entry_ref| / entry_ref over the legs
    overshoot_fraction: float | None = None  # (exit_move - tp) / (liq - tp)

    @property
    def legs(self) -> tuple[Leg, Leg]:
        return self.long, self.short

    @property
    def active(self) -> bool:
        return self.state in (RoundState.OPENING.value, RoundState.HEDGED.value, RoundState.CLOSING.value)

    def filled_position_ids(self) -> list[int]:
        return [leg.position_id for leg in self.legs if leg.status == LegStatus.FILLED.value and leg.position_id is not None]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Round":
        data = dict(data)
        data["long"] = Leg(**data["long"])
        data["short"] = Leg(**data["short"])
        data["closes"] = [CloseRecord(**c) for c in data.get("closes", [])]
        return cls(**data)


def close_overshoot(r: Round) -> tuple[float | None, float | None]:
    """(exit move, overshoot fraction) of a hedged round's close.

    The fraction is how far past the take-profit the exit fills landed, as a
    share of the take-profit-to-liquidation gap; a liquidated loser counts as
    at least 1.0 (the whole gap was crossed before the close landed).
    """
    if not r.became_hedged or not r.entry_ref:
        return None, None
    moves = [abs(leg.exit_price - r.entry_ref) / r.entry_ref for leg in r.legs
             if leg.exit_price is not None and leg.status != LegStatus.LIQUIDATED.value]
    exit_move = max(moves) if moves else None
    if r.liq_move is None or r.liq_move <= r.tp_move:
        return exit_move, None
    fraction = max(0.0, ((exit_move or r.tp_move) - r.tp_move) / (r.liq_move - r.tp_move))
    if r.liquidated:
        fraction = max(fraction, 1.0)
    return exit_move, fraction
