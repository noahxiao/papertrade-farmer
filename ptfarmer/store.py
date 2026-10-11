"""Persistence: SQLite (state + history) and JSONL (append-only audit log).

Intents are written *before* they are submitted (write-ahead), so a crash
between signing and the relayer's reply still leaves a record the next
process can reconcile against the account stream.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS rounds_state ON rounds(state);
CREATE TABLE IF NOT EXISTS intents (
    intent_id TEXT PRIMARY KEY,
    round_id INTEGER,
    purpose TEXT NOT NULL,
    action TEXT NOT NULL,
    nonce TEXT NOT NULL,
    deadline INTEGER NOT NULL,
    status TEXT NOT NULL,
    code TEXT,
    position_ids TEXT,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS intents_round ON intents(round_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    kind TEXT NOT NULL,
    round_id INTEGER,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS stakes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    amount_paper REAL NOT NULL,
    intent_id TEXT,
    status TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS samples (
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS samples_kind_ts ON samples(kind, ts);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

ACTIVE_ROUND_STATES = ("OPENING", "HEDGED", "CLOSING")


class Store:
    def __init__(self, data_dir: str | Path, clock=time.time):
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(self.dir / "farmer.sqlite3", isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._events_path = self.dir / "events.jsonl"
        self._rounds_path = self.dir / "rounds.jsonl"

    def close(self) -> None:
        self.db.close()

    def _append_jsonl(self, path: Path, record: dict) -> None:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str, sort_keys=True) + "\n")
            fh.flush()

    # rounds -----------------------------------------------------------------
    def insert_round(self, market: str, state: str, data: dict) -> int:
        now = self.clock()
        cur = self.db.execute(
            "INSERT INTO rounds (market, state, created_at, updated_at, data) VALUES (?, ?, ?, ?, ?)",
            (market, state, now, now, json.dumps(data)),
        )
        return int(cur.lastrowid)

    def update_round(self, round_id: int, state: str, data: dict, closed: bool = False) -> None:
        now = self.clock()
        self.db.execute(
            "UPDATE rounds SET state = ?, updated_at = ?, data = ?, closed_at = COALESCE(closed_at, ?) WHERE id = ?",
            (state, now, json.dumps(data), now if closed else None, round_id),
        )
        if closed:
            self._append_jsonl(self._rounds_path, {"id": round_id, "state": state, "closed_at": now, **data})

    def active_rounds(self) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in ACTIVE_ROUND_STATES)
        return list(self.db.execute(f"SELECT * FROM rounds WHERE state IN ({marks}) ORDER BY id", ACTIVE_ROUND_STATES))

    def rounds_closed_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return list(
            self.db.execute(
                "SELECT * FROM rounds WHERE closed_at IS NOT NULL AND closed_at >= ? AND closed_at < ? ORDER BY id",
                (start, end),
            )
        )

    def rounds_created_between(self, start: float, end: float) -> list[sqlite3.Row]:
        return list(
            self.db.execute("SELECT * FROM rounds WHERE created_at >= ? AND created_at < ? ORDER BY id", (start, end))
        )

    def last_closed_at(self, market: str) -> float | None:
        row = self.db.execute("SELECT MAX(closed_at) AS t FROM rounds WHERE market = ?", (market,)).fetchone()
        return row["t"] if row and row["t"] is not None else None

    # intents ----------------------------------------------------------------
    def record_intent(
        self,
        intent_id: str,
        round_id: int | None,
        purpose: str,
        action: str,
        nonce: int,
        deadline: int,
        payload: dict,
        position_ids: Iterable[int] | None = None,
    ) -> None:
        now = self.clock()
        self.db.execute(
            "INSERT OR REPLACE INTO intents (intent_id, round_id, purpose, action, nonce, deadline, status, code,"
            " position_ids, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'prepared', NULL, ?, ?, ?, ?)",
            (
                intent_id,
                round_id,
                purpose,
                action,
                str(nonce),
                deadline,
                json.dumps(list(position_ids)) if position_ids is not None else None,
                json.dumps(payload),
                now,
                now,
            ),
        )

    def set_intent_status(self, intent_id: str, status: str, code: str | None = None) -> None:
        self.db.execute(
            "UPDATE intents SET status = ?, code = COALESCE(?, code), updated_at = ? WHERE intent_id = ?",
            (status, code, self.clock(), intent_id),
        )

    def rename_intent(self, old_id: str, new_id: str) -> None:
        self.db.execute("UPDATE intents SET intent_id = ? WHERE intent_id = ?", (new_id, old_id))

    def intent(self, intent_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM intents WHERE intent_id = ?", (intent_id,)).fetchone()

    def intents_with_status(self, *statuses: str) -> list[sqlite3.Row]:
        marks = ",".join("?" for _ in statuses)
        return list(self.db.execute(f"SELECT * FROM intents WHERE status IN ({marks})", statuses))

    # events -----------------------------------------------------------------
    def log_event(self, kind: str, level: str = "info", round_id: int | None = None, **data: Any) -> None:
        now = self.clock()
        self.db.execute(
            "INSERT INTO events (ts, level, kind, round_id, data) VALUES (?, ?, ?, ?, ?)",
            (now, level, kind, round_id, json.dumps(data, default=str)),
        )
        self._append_jsonl(self._events_path, {"ts": now, "level": level, "kind": kind, "round_id": round_id, **data})

    def events_between(self, start: float, end: float, kinds: Iterable[str] | None = None) -> list[sqlite3.Row]:
        rows = self.db.execute("SELECT * FROM events WHERE ts >= ? AND ts < ? ORDER BY id", (start, end))
        wanted = set(kinds) if kinds else None
        return [r for r in rows if wanted is None or r["kind"] in wanted]

    # stakes -----------------------------------------------------------------
    def record_stake(self, amount_paper: float, status: str, intent_id: str | None = None, detail: str = "") -> None:
        self.db.execute(
            "INSERT INTO stakes (ts, amount_paper, intent_id, status, detail) VALUES (?, ?, ?, ?, ?)",
            (self.clock(), amount_paper, intent_id, status, detail),
        )
        self._append_jsonl(
            self._events_path,
            {"ts": self.clock(), "kind": "stake", "amount_paper": amount_paper, "status": status, "intent_id": intent_id},
        )

    def staked_between(self, start: float, end: float) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(amount_paper), 0) AS s FROM stakes WHERE status = 'accepted' AND ts >= ? AND ts < ?",
            (start, end),
        ).fetchone()
        return float(row["s"])

    # samples ----------------------------------------------------------------
    def add_sample(self, kind: str, data: dict, ts: float | None = None) -> None:
        self.db.execute(
            "INSERT INTO samples (ts, kind, data) VALUES (?, ?, ?)",
            (self.clock() if ts is None else ts, kind, json.dumps(data)),
        )

    def samples(self, kind: str, since: float) -> list[tuple[float, dict]]:
        rows = self.db.execute("SELECT ts, data FROM samples WHERE kind = ? AND ts >= ? ORDER BY ts", (kind, since))
        return [(r["ts"], json.loads(r["data"])) for r in rows]

    def prune_samples(self, older_than: float) -> None:
        self.db.execute("DELETE FROM samples WHERE ts < ?", (older_than,))

    # kv ---------------------------------------------------------------------
    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(value)))

    def kv_delete(self, key: str) -> None:
        self.db.execute("DELETE FROM kv WHERE key = ?", (key,))

    def next_nonce(self, scope: str, seed: int) -> int:
        """Atomically allocate a nonce from a persistent per-scope counter."""
        key = f"nonce:{scope}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            row = self.db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
            current = int(json.loads(row["value"])) if row else seed
            self.db.execute("INSERT OR REPLACE INTO kv (key, value) VALUES (?, ?)", (key, json.dumps(str(current + 1))))
            self.db.execute("COMMIT")
        except Exception:
            self.db.execute("ROLLBACK")
            raise
        return current
