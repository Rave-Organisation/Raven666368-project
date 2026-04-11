"""
Alpha Engine — Priority Fee Oracle
=====================================
Queries the Solana RPC for recent prioritisation fee data and computes
a statistically-grounded compute-unit price recommendation.

Two use-paths
-------------
1. Standalone:
       oracle = PriorityFeeOracle(rpc_url)
       price  = await oracle.recommend(urgency="high")

2. SQLite bridge for the Rust execution hot path:
       oracle writes the recommended fee into a "fee_cache" table;
       the Rust bridge reads it every N ms without async Python overhead.

Output: compute unit price in micro-lamports (µL).
    Total priority fee = compute_unit_price_µL * compute_units / 1_000_000
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import aiohttp

from engine.infrastructure.logger import get_logger

log = get_logger(__name__)

Urgency = Literal["low", "normal", "high", "critical"]

MIN_CU_PRICE_MICRO_LAMPORTS: int = 1_000
MAX_CU_PRICE_MICRO_LAMPORTS: int = 5_000_000


@dataclass(frozen=True)
class FeeRecommendation:
    urgency:             Urgency
    recommended_µL:      int
    p50_µL:              int
    p75_µL:              int
    p90_µL:              int
    p99_µL:              int
    sample_count:        int
    sampled_at:          datetime
    estimated_total_sol: float

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.sampled_at).total_seconds()

    def __str__(self) -> str:
        return (
            f"FeeRec[{self.urgency}] "
            f"rec={self.recommended_µL:,} µL/CU  "
            f"p50={self.p50_µL:,}  p75={self.p75_µL:,}  p90={self.p90_µL:,}  "
            f"~{self.estimated_total_sol:.6f} SOL  "
            f"(n={self.sample_count})"
        )


class PriorityFeeOracle:
    """
    Queries `getRecentPrioritizationFees` across AMM program IDs and
    computes percentile-based fee recommendations.
    """

    DEFAULT_ACCOUNTS = [
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",  # Raydium AMM v4
        "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",  # Jupiter V6
        "9W959DqEETiGZocYWCQPaJ6sBmUzgfxXfqGeTEdp3aQP", # Orca Whirlpool
    ]

    def __init__(
        self,
        rpc_url:           str,
        accounts:          list[str] | None = None,
        cache_ttl_seconds: float = 2.0,
        typical_cu:        int   = 200_000,
        db_path:           str | Path | None = None,
    ) -> None:
        self._rpc_url    = rpc_url
        self._accounts   = accounts or self.DEFAULT_ACCOUNTS
        self._cache_ttl  = cache_ttl_seconds
        self._typical_cu = typical_cu
        self._db_path    = Path(db_path) if db_path else None
        self._cache: FeeRecommendation | None = None
        self._request_id = 0

        if self._db_path:
            self._init_db()

    async def recommend(self, urgency: Urgency = "normal") -> FeeRecommendation:
        if self._cache and self._cache.age_seconds < self._cache_ttl:
            return self._reselect(self._cache, urgency)
        fees = await self._fetch_fees()
        if not fees:
            log.warning("PriorityFeeOracle: no fee data; using fallback defaults.")
            return self._fallback(urgency)
        rec = self._compute(fees, urgency)
        self._cache = rec
        if self._db_path:
            self._write_to_db(rec)
        log.debug("PriorityFeeOracle: %s", rec)
        return rec

    async def poll_forever(self, interval_seconds: float = 2.0) -> None:
        log.info("PriorityFeeOracle: background poller started (interval=%.1fs).", interval_seconds)
        while True:
            try:
                await self.recommend("normal")
            except Exception as exc:
                log.warning("PriorityFeeOracle: poll error — %s", exc)
            await asyncio.sleep(interval_seconds)

    async def _fetch_fees(self) -> list[int]:
        self._request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id":      self._request_id,
            "method":  "getRecentPrioritizationFees",
            "params":  [self._accounts],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc_url,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        log.warning("PriorityFeeOracle: RPC HTTP %d", resp.status)
                        return []
                    data = await resp.json()
            if "error" in data:
                log.warning("PriorityFeeOracle: RPC error — %s", data["error"])
                return []
            results = data.get("result", [])
            return [int(r["prioritizationFee"]) for r in results if r.get("prioritizationFee", 0) > 0]
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("PriorityFeeOracle: network error — %s", exc)
            return []

    def _compute(self, fees: list[int], urgency: Urgency) -> FeeRecommendation:
        sorted_fees = sorted(fees)
        n = len(sorted_fees)

        def pct(p: float) -> int:
            idx = min(int(p / 100 * n), n - 1)
            return sorted_fees[idx]

        p50 = max(MIN_CU_PRICE_MICRO_LAMPORTS, pct(50))
        p75 = max(MIN_CU_PRICE_MICRO_LAMPORTS, pct(75))
        p90 = max(MIN_CU_PRICE_MICRO_LAMPORTS, pct(90))
        p99 = max(MIN_CU_PRICE_MICRO_LAMPORTS, min(MAX_CU_PRICE_MICRO_LAMPORTS, pct(99)))

        recommended = self._select_by_urgency(urgency, p50, p75, p90, p99)
        total_sol   = recommended * self._typical_cu / 1_000_000 / 1_000_000_000

        return FeeRecommendation(
            urgency=urgency, recommended_µL=recommended,
            p50_µL=p50, p75_µL=p75, p90_µL=p90, p99_µL=p99,
            sample_count=n,
            sampled_at=datetime.now(timezone.utc),
            estimated_total_sol=total_sol,
        )

    def _reselect(self, cached: FeeRecommendation, urgency: Urgency) -> FeeRecommendation:
        recommended = self._select_by_urgency(urgency, cached.p50_µL, cached.p75_µL, cached.p90_µL, cached.p99_µL)
        total_sol = recommended * self._typical_cu / 1_000_000 / 1_000_000_000
        return FeeRecommendation(
            urgency=urgency, recommended_µL=recommended,
            p50_µL=cached.p50_µL, p75_µL=cached.p75_µL, p90_µL=cached.p90_µL, p99_µL=cached.p99_µL,
            sample_count=cached.sample_count,
            sampled_at=cached.sampled_at,
            estimated_total_sol=total_sol,
        )

    @staticmethod
    def _select_by_urgency(urgency: Urgency, p50: int, p75: int, p90: int, p99: int) -> int:
        return {"low": p50, "normal": p75, "high": p90, "critical": p99}[urgency]

    def _fallback(self, urgency: Urgency) -> FeeRecommendation:
        defaults = {"low": 5_000, "normal": 25_000, "high": 100_000, "critical": 500_000}
        rec = defaults[urgency]
        total_sol = rec * self._typical_cu / 1_000_000 / 1_000_000_000
        return FeeRecommendation(
            urgency=urgency, recommended_µL=rec,
            p50_µL=5_000, p75_µL=25_000, p90_µL=100_000, p99_µL=500_000,
            sample_count=0,
            sampled_at=datetime.now(timezone.utc),
            estimated_total_sol=total_sol,
        )

    _FEE_SCHEMA = """
    CREATE TABLE IF NOT EXISTS fee_cache (
        id            INTEGER PRIMARY KEY CHECK (id = 1),
        urgency       TEXT,
        recommended   INTEGER,
        p50           INTEGER,
        p75           INTEGER,
        p90           INTEGER,
        p99           INTEGER,
        sample_count  INTEGER,
        updated_at    TEXT
    );
    """

    def _init_db(self) -> None:
        conn = sqlite3.connect(self._db_path)
        conn.executescript(self._FEE_SCHEMA)
        conn.commit()
        conn.close()

    def _write_to_db(self, rec: FeeRecommendation) -> None:
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """
                INSERT INTO fee_cache (id, urgency, recommended, p50, p75, p90, p99, sample_count, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    urgency=excluded.urgency, recommended=excluded.recommended,
                    p50=excluded.p50, p75=excluded.p75,
                    p90=excluded.p90, p99=excluded.p99,
                    sample_count=excluded.sample_count, updated_at=excluded.updated_at
                """,
                (
                    rec.urgency, rec.recommended_µL,
                    rec.p50_µL, rec.p75_µL, rec.p90_µL, rec.p99_µL,
                    rec.sample_count, rec.sampled_at.isoformat(),
                ),
            )
            conn.commit()
            conn.close()
        except sqlite3.Error as exc:
            log.warning("PriorityFeeOracle: SQLite write error — %s", exc)


async def simulate_cu_usage(rpc_url: str, transaction_b64: str) -> int | None:
    """
    Calls simulateTransaction to get the actual CU consumed before sending,
    so you can set a tight ComputeBudget instruction instead of defaulting to 200k.
    """
    payload = {
        "jsonrpc": "2.0",
        "id":      1,
        "method":  "simulateTransaction",
        "params":  [transaction_b64, {"replaceRecentBlockhash": True, "commitment": "processed", "encoding": "base64"}],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(rpc_url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                data = await resp.json()
        consumed = data["result"]["value"].get("unitsConsumed")
        if consumed:
            return int(consumed * 1.1)
    except Exception as exc:
        log.warning("simulate_cu_usage: failed — %s", exc)
    return None
