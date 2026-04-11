"""
Alpha Engine — Data Loader
============================
Provides a uniform MarketEvent stream from three sources:

  SQLiteDataLoader    — replays the bot's own audit trail
  CSVDataLoader       — ingests Birdeye / Helius CSV exports
  LiveWebSocketLoader — streams real-time price ticks for paper trading

All loaders expose a synchronous `stream()` generator and an async
`stream_async()` generator so the harness can use the same code paths.
"""

from __future__ import annotations

import asyncio
import csv
import json
import sqlite3
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Generator, Optional

from engine.infrastructure.logger import get_logger

log = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Core event type
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class MarketEvent:
    """
    Immutable snapshot of a single market tick.
    The `raw` field carries source-specific metadata (on-chain accounts,
    OSINT tags, sentiment scores) that the classifier can inspect.
    """
    timestamp:    datetime
    token_mint:   str
    price:        float
    volume_sol:   float
    liquidity_sol: float
    raw:          dict[str, Any] = field(default_factory=dict, hash=False, compare=False)

    @property
    def age_seconds(self) -> float:
        first_seen = self.raw.get("first_seen_ts")
        if first_seen is None:
            return 0.0
        if isinstance(first_seen, (int, float)):
            fs = datetime.fromtimestamp(first_seen, tz=timezone.utc)
        else:
            fs = first_seen
        return (self.timestamp - fs).total_seconds()


# ──────────────────────────────────────────────────────────────────────────────
# Abstract base
# ──────────────────────────────────────────────────────────────────────────────

class DataLoader(ABC):

    @abstractmethod
    def stream(self) -> Generator[MarketEvent, None, None]:
        ...

    async def stream_async(self) -> AsyncGenerator[MarketEvent, None]:
        for event in self.stream():
            yield event
            await asyncio.sleep(0)


# ──────────────────────────────────────────────────────────────────────────────
# SQLite loader
# ──────────────────────────────────────────────────────────────────────────────

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_ticks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    token_mint     TEXT    NOT NULL,
    price_sol      REAL    NOT NULL,
    volume_sol     REAL    NOT NULL DEFAULT 0,
    liquidity_sol  REAL    NOT NULL DEFAULT 0,
    raw_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_ticks_ts    ON market_ticks (ts);
CREATE INDEX IF NOT EXISTS idx_ticks_mint  ON market_ticks (token_mint);
"""


class SQLiteDataLoader(DataLoader):
    """
    Replays MarketEvents from the bot's local SQLite audit database.
    Ideal for closed-loop validation: the same data the live bot saw.
    """

    def __init__(
        self,
        db_path:     str | Path,
        start_time:  str | None = None,
        end_time:    str | None = None,
        token_mints: list[str] | None = None,
        batch_size:  int = 1000,
    ) -> None:
        self._db_path    = Path(db_path)
        self._start_time = start_time
        self._end_time   = end_time
        self._mints      = token_mints
        self._batch_size = batch_size

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(_SQLITE_SCHEMA)
        conn.commit()

    def stream(self) -> Generator[MarketEvent, None, None]:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        self._ensure_schema(conn)

        query = "SELECT ts, token_mint, price_sol, volume_sol, liquidity_sol, raw_json FROM market_ticks WHERE 1=1"
        params: list[Any] = []

        if self._start_time:
            query += " AND ts >= ?"
            params.append(self._start_time)
        if self._end_time:
            query += " AND ts <= ?"
            params.append(self._end_time)
        if self._mints:
            placeholders = ",".join("?" * len(self._mints))
            query += f" AND token_mint IN ({placeholders})"
            params.extend(self._mints)

        query += " ORDER BY ts ASC"

        total = 0
        cursor = conn.execute(query, params)
        while True:
            rows = cursor.fetchmany(self._batch_size)
            if not rows:
                break
            for row in rows:
                raw = json.loads(row["raw_json"]) if row["raw_json"] else {}
                yield MarketEvent(
                    timestamp     = datetime.fromisoformat(row["ts"]).replace(tzinfo=timezone.utc),
                    token_mint    = row["token_mint"],
                    price         = float(row["price_sol"]),
                    volume_sol    = float(row["volume_sol"]),
                    liquidity_sol = float(row["liquidity_sol"]),
                    raw           = raw,
                )
                total += 1

        conn.close()
        log.info("SQLiteDataLoader: yielded %d events.", total)


# ──────────────────────────────────────────────────────────────────────────────
# CSV loader  (Birdeye / Helius exports)
# ──────────────────────────────────────────────────────────────────────────────

class CSVDataLoader(DataLoader):
    """
    Loads tick data from a CSV file.
    Column names are configurable via `column_map`.
    """

    DEFAULT_MAP = {
        "ts":            "ts",
        "token_mint":    "token_mint",
        "price_sol":     "price_sol",
        "volume_sol":    "volume_sol",
        "liquidity_sol": "liquidity_sol",
    }

    def __init__(
        self,
        csv_path:   str | Path,
        column_map: dict[str, str] | None = None,
        delimiter:  str = ",",
    ) -> None:
        self._path      = Path(csv_path)
        self._col       = {**self.DEFAULT_MAP, **(column_map or {})}
        self._delimiter = delimiter

    def stream(self) -> Generator[MarketEvent, None, None]:
        total = 0
        with open(self._path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh, delimiter=self._delimiter)
            for row in reader:
                try:
                    ts_raw = row[self._col["ts"]]
                    if ts_raw.replace(".", "").isdigit():
                        ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(ts_raw.rstrip("Z")).replace(tzinfo=timezone.utc)

                    yield MarketEvent(
                        timestamp     = ts,
                        token_mint    = row[self._col["token_mint"]],
                        price         = float(row.get(self._col["price_sol"], 0)),
                        volume_sol    = float(row.get(self._col["volume_sol"], 0)),
                        liquidity_sol = float(row.get(self._col["liquidity_sol"], 0)),
                        raw           = dict(row),
                    )
                    total += 1
                except (KeyError, ValueError) as exc:
                    log.warning("CSVDataLoader: skipping malformed row — %s", exc)

        log.info("CSVDataLoader: yielded %d events from %s.", total, self._path.name)


# ──────────────────────────────────────────────────────────────────────────────
# Live WebSocket loader  (paper trading / shadow mode)
# ──────────────────────────────────────────────────────────────────────────────

class LiveWebSocketLoader(DataLoader):
    """
    Streams real-time MarketEvents from a Helius or custom WebSocket feed.
    Expected message format: { "mint": str, "price": float, "ts": int (unix ms), ... }
    """

    def __init__(
        self,
        ws_url:      str,
        token_mints: list[str] | None = None,
        max_events:  int | None = None,
    ) -> None:
        self._ws_url     = ws_url
        self._mints      = set(token_mints) if token_mints else None
        self._max_events = max_events

    def stream(self) -> Generator[MarketEvent, None, None]:
        loop = asyncio.new_event_loop()
        gen  = self.stream_async()
        try:
            while True:
                event = loop.run_until_complete(gen.__anext__())
                yield event
        except StopAsyncIteration:
            pass
        finally:
            loop.close()

    async def stream_async(self) -> AsyncGenerator[MarketEvent, None]:
        try:
            import websockets  # type: ignore
        except ImportError:
            raise RuntimeError("Install websockets: pip install websockets")

        count = 0
        log.info("LiveWebSocketLoader: connecting to %s", self._ws_url)
        async with websockets.connect(self._ws_url) as ws:
            async for raw_msg in ws:
                try:
                    data = json.loads(raw_msg)
                    mint = data.get("mint") or data.get("token_mint", "")
                    if self._mints and mint not in self._mints:
                        continue

                    ts_ms = data.get("ts") or data.get("blockTime", time.time() * 1000)
                    ts    = datetime.fromtimestamp(float(ts_ms) / 1000, tz=timezone.utc)

                    yield MarketEvent(
                        timestamp     = ts,
                        token_mint    = mint,
                        price         = float(data.get("price", 0)),
                        volume_sol    = float(data.get("volume", 0)),
                        liquidity_sol = float(data.get("liquidity", 0)),
                        raw           = data,
                    )
                    count += 1
                    if self._max_events and count >= self._max_events:
                        log.info("LiveWebSocketLoader: max_events=%d reached.", self._max_events)
                        return
                except (json.JSONDecodeError, KeyError, ValueError) as exc:
                    log.warning("LiveWebSocketLoader: bad message — %s", exc)
