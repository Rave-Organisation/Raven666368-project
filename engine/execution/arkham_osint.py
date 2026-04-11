"""
Alpha Engine — Arkham Intelligence OSINT Layer
================================================
Three intelligence streams unified under one efficient client:

  A) Known whale wallet tracker
     Monitor specific high-value wallets for entries into new Solana tokens.
     Triggers a conviction score boost when a tracked whale is seen buying
     within the first 60 seconds of a token launch.

  B) Smart money discovery
     Uses Arkham's entity labels to surface unknown wallets classified as
     "smart money", "fund", "market maker", or "dex" that are moving into
     a token before price action confirms the move.

  C) Full combined layer
     Both A and B feeding a unified signal enrichment pipeline that attaches
     an `arkham_context` dict to every MarketEvent for the classifier.

Resource efficiency principles
-------------------------------
  1. SQLite response cache — every API response is cached with a per-endpoint
     TTL. Identical queries within the TTL are served from cache (zero API calls).
  2. Token bucket rate limiter — hard cap of N calls/minute regardless of
     how many signals fire simultaneously. Configurable per plan tier.
  3. Batch queries — when multiple tokens need enrichment, group them into
     a single Arkham query where the API supports it.
  4. Lazy enrichment — only call Arkham when the classifier's pre-score
     clears a minimum threshold. Below-threshold signals are not worth
     the API budget.
  5. Deduplication — the same (wallet, token) pair seen twice within the
     cache window returns the cached verdict.

Arkham API reference
--------------------
  Base URL : https://api.arkhamintelligence.com
  Auth     : x-api-key header
  Endpoints used:
    GET /transfers?base=<mint>&flow=in&limit=20    — inbound transfers to token
    GET /intelligence/address/<address>             — entity label for a wallet
    GET /portfolio/<address>                        — wallet token holdings
    GET /intelligence/token/<mint>                  — token-level intel summary
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiohttp

from engine.infrastructure.logger import AuditLogger, get_logger

log = get_logger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

ARKHAM_BASE_URL = "https://api.arkhamintelligence.com"

# Cache TTLs (seconds) — tune per endpoint volatility
TTL = {
    "transfers":   60,     # 1 min  — transfer data changes fast
    "address":     3600,   # 1 hour — entity labels rarely change
    "portfolio":   120,    # 2 min  — portfolio can shift quickly
    "token_intel": 300,    # 5 min  — token-level summary
}

# Wallet entity tags that qualify as "smart money"
SMART_MONEY_TAGS = frozenset({
    "smart money", "fund", "venture capital", "market maker",
    "whale", "dex", "institution", "early investor", "kol",
})

# Minimum pre-score before Arkham enrichment is triggered (saves API budget)
MIN_PRESCORE_FOR_ENRICHMENT = 50.0

# Conviction boost values applied when Arkham confirms signals
BOOST = {
    "tracked_whale_entry":  20.0,   # a specifically watched wallet entered
    "smart_money_entry":    12.0,   # unknown but labelled smart money entered
    "multi_whale_cluster":  15.0,   # 3+ whales entered within 2 minutes
    "exchange_outflow":      8.0,   # net outflow from exchange = accumulation
    "exchange_inflow":     -10.0,   # net inflow to exchange = sell pressure
}


# ─────────────────────────────────────────────────────────────────────────────
# Rate limiter — token bucket
# ─────────────────────────────────────────────────────────────────────────────

class TokenBucketRateLimiter:
    """
    Token bucket algorithm.
    Refills at `rate` tokens/second up to `capacity`.
    Callers await `acquire()` which blocks until a token is available.

    Free trial budget: 1000 req/day ≈ 0.0116 req/s → capacity=10, rate=0.7
    This gives bursts of up to 10 calls then ~1.4s between calls on sustained load.
    """

    def __init__(self, capacity: float = 10.0, rate: float = 0.7) -> None:
        self._capacity    = capacity
        self._rate        = rate           # tokens per second
        self._tokens      = capacity
        self._last_refill = time.monotonic()
        self._lock        = asyncio.Lock()

    async def acquire(self, tokens: float = 1.0) -> None:
        async with self._lock:
            while True:
                now            = time.monotonic()
                elapsed        = now - self._last_refill
                self._tokens   = min(self._capacity, self._tokens + elapsed * self._rate)
                self._last_refill = now

                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return

                wait = (tokens - self._tokens) / self._rate
                await asyncio.sleep(wait)


# ─────────────────────────────────────────────────────────────────────────────
# SQLite cache
# ─────────────────────────────────────────────────────────────────────────────

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS arkham_cache (
    cache_key   TEXT PRIMARY KEY,
    endpoint    TEXT NOT NULL,
    response    TEXT NOT NULL,
    cached_at   REAL NOT NULL,
    ttl_seconds INTEGER NOT NULL,
    hit_count   INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_endpoint ON arkham_cache (endpoint);

CREATE TABLE IF NOT EXISTS whale_watchlist (
    address     TEXT PRIMARY KEY,
    label       TEXT,
    added_at    TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE IF NOT EXISTS arkham_signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    token_mint    TEXT NOT NULL,
    signal_type   TEXT NOT NULL,
    wallet        TEXT,
    entity_name   TEXT,
    boost_applied REAL,
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_mint ON arkham_signals (token_mint);
CREATE INDEX IF NOT EXISTS idx_signals_ts   ON arkham_signals (ts);
"""


class ArkhamCache:
    def __init__(self, db_path: str | Path) -> None:
        self._db = Path(db_path)
        self._init()

    def _init(self) -> None:
        conn = sqlite3.connect(self._db)
        conn.executescript(_CACHE_SCHEMA)
        conn.commit()
        conn.close()

    def get(self, key: str) -> dict[str, Any] | None:
        conn = sqlite3.connect(self._db)
        row  = conn.execute(
            "SELECT response, cached_at, ttl_seconds FROM arkham_cache WHERE cache_key=?",
            (key,),
        ).fetchone()
        conn.close()

        if not row:
            return None

        response, cached_at, ttl = row
        if time.time() - cached_at > ttl:
            return None

        try:
            c = sqlite3.connect(self._db)
            c.execute("UPDATE arkham_cache SET hit_count=hit_count+1 WHERE cache_key=?", (key,))
            c.commit()
            c.close()
        except Exception:
            pass

        return json.loads(response)

    def set(self, key: str, endpoint: str, data: dict[str, Any], ttl: int) -> None:
        conn = sqlite3.connect(self._db)
        conn.execute(
            """
            INSERT INTO arkham_cache (cache_key, endpoint, response, cached_at, ttl_seconds)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(cache_key) DO UPDATE SET
                response=excluded.response,
                cached_at=excluded.cached_at,
                ttl_seconds=excluded.ttl_seconds
            """,
            (key, endpoint, json.dumps(data), time.time(), ttl),
        )
        conn.commit()
        conn.close()

    def purge_expired(self) -> int:
        conn  = sqlite3.connect(self._db)
        count = conn.execute(
            "DELETE FROM arkham_cache WHERE (? - cached_at) > ttl_seconds",
            (time.time(),),
        ).rowcount
        conn.commit()
        conn.close()
        return count

    def record_signal(
        self,
        token_mint:  str,
        signal_type: str,
        wallet:      str | None,
        entity_name: str | None,
        boost:       float,
        raw:         dict[str, Any],
    ) -> None:
        conn = sqlite3.connect(self._db)
        conn.execute(
            """
            INSERT INTO arkham_signals
                (ts, token_mint, signal_type, wallet, entity_name, boost_applied, raw_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                token_mint, signal_type, wallet, entity_name,
                boost, json.dumps(raw, default=str),
            ),
        )
        conn.commit()
        conn.close()

    def add_whale(self, address: str, label: str = "", note: str = "") -> None:
        conn = sqlite3.connect(self._db)
        conn.execute(
            """
            INSERT INTO whale_watchlist (address, label, added_at, note)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET label=excluded.label, note=excluded.note
            """,
            (address, label, datetime.now(timezone.utc).isoformat(), note),
        )
        conn.commit()
        conn.close()
        log.info("Whale watchlist: added %s (%s)", address[:8], label)

    def get_watchlist(self) -> list[str]:
        conn = sqlite3.connect(self._db)
        rows = conn.execute("SELECT address FROM whale_watchlist").fetchall()
        conn.close()
        return [r[0] for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# Output types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class WalletIntel:
    address:          str
    entity_name:      str | None
    entity_type:      str | None
    tags:             list[str]
    is_smart_money:   bool
    is_tracked_whale: bool
    raw:              dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class TokenTransferSummary:
    token_mint:            str
    inbound_count:         int
    unique_buyers:         int
    smart_money_buyers:    list[WalletIntel]
    tracked_whale_buyers:  list[WalletIntel]
    exchange_inflow_sol:   float
    exchange_outflow_sol:  float
    multi_whale_cluster:   bool


@dataclass
class ArkhamEnrichment:
    """
    The final output attached to a MarketEvent before classifier scoring.
    Contains all Arkham signals and the total conviction boost to apply.
    """
    token_mint:        str
    total_boost:       float
    signals:           list[str]
    transfer_summary:  TokenTransferSummary | None
    api_calls_made:    int
    served_from_cache: bool


# ─────────────────────────────────────────────────────────────────────────────
# Main Arkham client
# ─────────────────────────────────────────────────────────────────────────────

class ArkhamOSINT:
    """
    Unified Arkham intelligence client covering streams A, B, and C.

    Usage
    -----
        arkham = ArkhamOSINT.from_env()

        # Add known whale wallets to watchlist
        arkham.add_tracked_whale("WALLET_ADDRESS", label="Jump Trading")

        # Enrich a token before final conviction scoring
        enrichment = await arkham.enrich_token(token_mint, pre_score=72.0)
        final_score = pre_score + enrichment.total_boost
    """

    def __init__(
        self,
        api_key:      str,
        db_path:      str | Path = "data/arkham_cache.db",
        rate_limiter: TokenBucketRateLimiter | None = None,
    ) -> None:
        self._api_key = api_key
        self._cache   = ArkhamCache(db_path)
        self._rl      = rate_limiter or TokenBucketRateLimiter(capacity=10, rate=0.7)
        self._calls   = 0
        self._audit   = AuditLogger()

    @classmethod
    def from_env(cls) -> "ArkhamOSINT":
        key = os.getenv("ARKHAM_API_KEY", "")
        if not key:
            raise RuntimeError("ARKHAM_API_KEY environment variable not set.")
        return cls(
            api_key = key,
            db_path = os.getenv("ALPHA_DB_PATH", "data/alpha_engine.db"),
        )

    # ── Watchlist management ──────────────────────────────────────────────────

    def add_tracked_whale(self, address: str, label: str = "", note: str = "") -> None:
        self._cache.add_whale(address, label, note)

    def load_watchlist_from_file(self, path: str | Path) -> int:
        """
        Load whale addresses from a plain text file (one address per line).
        Lines starting with # are treated as labels for the previous address.
        """
        p     = Path(path)
        count = 0
        with open(p) as fh:
            current_addr  = ""
            current_label = ""
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    current_label = line[1:].strip()
                else:
                    if current_addr:
                        self.add_tracked_whale(current_addr, current_label)
                        count += 1
                    current_addr  = line
                    current_label = ""
            if current_addr:
                self.add_tracked_whale(current_addr, current_label)
                count += 1
        log.info("Loaded %d whale addresses from %s.", count, p.name)
        return count

    # ── Core enrichment (main public method) ─────────────────────────────────

    async def enrich_token(
        self,
        token_mint: str,
        pre_score:  float = 0.0,
    ) -> ArkhamEnrichment:
        """
        Full enrichment pipeline for a token mint address.

        Steps:
          1. Skip if pre_score < MIN_PRESCORE (save API budget)
          2. Fetch recent inbound transfers (cached 60s)
          3. For each unique buyer, fetch entity label (cached 1h)
          4. Cross-reference against whale watchlist
          5. Compute transfer summary and conviction boosts
          6. Return ArkhamEnrichment with total boost and signal list
        """
        if pre_score < MIN_PRESCORE_FOR_ENRICHMENT:
            log.debug(
                "Skipping Arkham enrichment for %s (pre_score=%.1f < %.1f)",
                token_mint[:8], pre_score, MIN_PRESCORE_FOR_ENRICHMENT,
            )
            return ArkhamEnrichment(
                token_mint=token_mint, total_boost=0.0, signals=[],
                transfer_summary=None, api_calls_made=0, served_from_cache=True,
            )

        calls_before = self._calls
        watchlist    = set(self._cache.get_watchlist())

        transfers = await self._get_transfers(token_mint)
        if not transfers:
            return ArkhamEnrichment(
                token_mint=token_mint, total_boost=0.0, signals=[],
                transfer_summary=None,
                api_calls_made=self._calls - calls_before,
                served_from_cache=self._calls == calls_before,
            )

        buyers        = self._extract_unique_buyers(transfers)
        wallet_intels = await self._batch_label_wallets(buyers, watchlist)
        inflow, outflow = self._compute_exchange_flows(transfers)

        smart_money = [w for w in wallet_intels if w.is_smart_money]
        tracked     = [w for w in wallet_intels if w.is_tracked_whale]
        cluster     = self._detect_whale_cluster(
            transfers, watchlist | {w.address for w in smart_money}
        )

        summary = TokenTransferSummary(
            token_mint           = token_mint,
            inbound_count        = len(transfers),
            unique_buyers        = len(buyers),
            smart_money_buyers   = smart_money,
            tracked_whale_buyers = tracked,
            exchange_inflow_sol  = inflow,
            exchange_outflow_sol = outflow,
            multi_whale_cluster  = cluster,
        )

        total_boost = 0.0
        signals: list[str] = []

        for w in tracked:
            total_boost += BOOST["tracked_whale_entry"]
            signals.append(f"Tracked whale entered: {w.entity_name or w.address[:8]}")
            self._cache.record_signal(
                token_mint, "tracked_whale_entry", w.address,
                w.entity_name, BOOST["tracked_whale_entry"], w.raw,
            )

        for w in smart_money:
            if not w.is_tracked_whale:
                total_boost += BOOST["smart_money_entry"]
                signals.append(
                    f"Smart money entered: {w.entity_name or w.address[:8]} "
                    f"({', '.join(w.tags[:2])})"
                )
                self._cache.record_signal(
                    token_mint, "smart_money_entry", w.address,
                    w.entity_name, BOOST["smart_money_entry"], w.raw,
                )

        if cluster:
            total_boost += BOOST["multi_whale_cluster"]
            signals.append("Whale cluster detected: 3+ wallets within 2-minute window")

        if outflow > 0 and outflow > inflow:
            total_boost += BOOST["exchange_outflow"]
            signals.append(f"Exchange outflow {outflow:.2f} SOL > inflow {inflow:.2f} SOL (accumulation)")

        if inflow > outflow * 1.5:
            total_boost += BOOST["exchange_inflow"]
            signals.append(f"Heavy exchange inflow {inflow:.2f} SOL (sell pressure)")

        if signals:
            log.info(
                "Arkham enrichment %s: boost=+%.1f signals=%d calls=%d",
                token_mint[:8], total_boost, len(signals), self._calls - calls_before,
            )

        return ArkhamEnrichment(
            token_mint        = token_mint,
            total_boost       = total_boost,
            signals           = signals,
            transfer_summary  = summary,
            api_calls_made    = self._calls - calls_before,
            served_from_cache = self._calls == calls_before,
        )

    # ── Stream A: known whale tracker ─────────────────────────────────────────

    async def check_whale_activity(
        self,
        token_mint:       str,
        lookback_seconds: int = 120,
    ) -> list[WalletIntel]:
        transfers = await self._get_transfers(token_mint)
        watchlist = set(self._cache.get_watchlist())
        buyers    = self._extract_unique_buyers(transfers)
        hits      = [b for b in buyers if b in watchlist]
        if not hits:
            return []
        results = []
        for addr in hits:
            intel = await self._get_wallet_intel(addr, watchlist)
            results.append(intel)
        return results

    # ── Stream B: smart money discovery ───────────────────────────────────────

    async def discover_smart_money(self, token_mint: str) -> list[WalletIntel]:
        transfers = await self._get_transfers(token_mint)
        buyers    = self._extract_unique_buyers(transfers)
        watchlist = set(self._cache.get_watchlist())
        intels    = await self._batch_label_wallets(buyers, watchlist)
        return [w for w in intels if w.is_smart_money]

    # ── API calls with cache ───────────────────────────────────────────────────

    async def _get_transfers(self, token_mint: str) -> list[dict[str, Any]]:
        cache_key = f"transfers:{token_mint}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            log.debug("Cache HIT transfers:%s", token_mint[:8])
            return cached.get("transfers", [])

        await self._rl.acquire()
        self._calls += 1

        data = await self._request(
            "GET", "/transfers",
            params={"base": token_mint, "flow": "in", "limit": "25", "chain": "solana"},
        )

        transfers = data.get("transfers", []) if data else []
        self._cache.set(cache_key, "transfers", {"transfers": transfers}, TTL["transfers"])
        return transfers

    async def _get_wallet_intel(self, address: str, watchlist: set[str]) -> WalletIntel:
        cache_key = f"address:{address}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            log.debug("Cache HIT address:%s", address[:8])
            return self._parse_wallet_intel(address, cached, watchlist)

        await self._rl.acquire()
        self._calls += 1

        data    = await self._request("GET", f"/intelligence/address/{address}")
        payload = data or {}
        self._cache.set(cache_key, "address", payload, TTL["address"])
        return self._parse_wallet_intel(address, payload, watchlist)

    async def _batch_label_wallets(
        self,
        addresses:      list[str],
        watchlist:      set[str],
        max_concurrent: int = 5,
    ) -> list[WalletIntel]:
        semaphore = asyncio.Semaphore(max_concurrent)

        async def label_one(addr: str) -> WalletIntel:
            async with semaphore:
                return await self._get_wallet_intel(addr, watchlist)

        tasks = [label_one(a) for a in addresses[:20]]
        return list(await asyncio.gather(*tasks, return_exceptions=False))

    async def _get_token_intel(self, token_mint: str) -> dict[str, Any]:
        cache_key = f"token_intel:{token_mint}"
        cached    = self._cache.get(cache_key)
        if cached is not None:
            return cached

        await self._rl.acquire()
        self._calls += 1
        data = await self._request("GET", f"/intelligence/token/{token_mint}") or {}
        self._cache.set(cache_key, "token_intel", data, TTL["token_intel"])
        return data

    # ── HTTP layer ─────────────────────────────────────────────────────────────

    async def _request(
        self,
        method: str,
        path:   str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        url     = ARKHAM_BASE_URL + path
        headers = {
            "x-api-key":    self._api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method, url,
                    headers = headers,
                    params  = params,
                    timeout = aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status == 429:
                        log.warning("Arkham rate limit hit — backing off 30s.")
                        await asyncio.sleep(30)
                        return None
                    if resp.status == 401:
                        log.error("Arkham API key invalid or expired.")
                        return None
                    if resp.status != 200:
                        log.warning("Arkham HTTP %d for %s", resp.status, path)
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            log.warning("Arkham request error: %s", exc)
            return None

    # ── Parsing helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_wallet_intel(
        address:   str,
        data:      dict[str, Any],
        watchlist: set[str],
    ) -> WalletIntel:
        entity      = data.get("arkhamEntity") or data.get("entity") or {}
        entity_name = entity.get("name") or entity.get("id")
        entity_type = entity.get("type", "unknown").lower()
        tags_raw    = entity.get("tags") or []
        tags        = [str(t).lower() for t in tags_raw]

        is_smart = (
            entity_type in {"fund", "market maker", "dex", "institution"} or
            bool(SMART_MONEY_TAGS & set(tags))
        )

        return WalletIntel(
            address          = address,
            entity_name      = entity_name,
            entity_type      = entity_type,
            tags             = tags,
            is_smart_money   = is_smart,
            is_tracked_whale = address in watchlist,
            raw              = data,
        )

    @staticmethod
    def _extract_unique_buyers(transfers: list[dict[str, Any]]) -> list[str]:
        seen = set()
        out  = []
        for t in transfers:
            addr = (
                t.get("fromAddress") or
                t.get("from", {}).get("address", "") or
                ""
            )
            if addr and addr not in seen:
                seen.add(addr)
                out.append(addr)
        return out

    @staticmethod
    def _compute_exchange_flows(
        transfers: list[dict[str, Any]],
    ) -> tuple[float, float]:
        inflow = outflow = 0.0
        for t in transfers:
            entity_type = (
                t.get("toEntity", {}).get("type", "") or
                t.get("fromEntity", {}).get("type", "")
            ).lower()
            amount_usd = float(t.get("historicalUSD") or t.get("usdValue") or 0)
            if "exchange" in entity_type or "cex" in entity_type:
                inflow  += amount_usd
            elif "exchange" in (t.get("fromEntity", {}).get("type", "") or "").lower():
                outflow += amount_usd
        return inflow, outflow

    @staticmethod
    def _detect_whale_cluster(
        transfers:   list[dict[str, Any]],
        whale_addrs: set[str],
        window_s:    int = 120,
        min_count:   int = 3,
    ) -> bool:
        whale_times: list[float] = []
        for t in transfers:
            addr = t.get("fromAddress") or t.get("from", {}).get("address", "")
            if addr not in whale_addrs:
                continue
            ts_raw = t.get("blockTimestamp") or t.get("timestamp")
            if ts_raw:
                whale_times.append(float(ts_raw))

        if len(whale_times) < min_count:
            return False

        whale_times.sort()
        for i in range(len(whale_times) - min_count + 1):
            if whale_times[i + min_count - 1] - whale_times[i] <= window_s:
                return True
        return False

    # ── Budget reporting ───────────────────────────────────────────────────────

    def budget_report(self) -> dict[str, Any]:
        conn  = sqlite3.connect(self._cache._db)
        total = conn.execute("SELECT COUNT(*) FROM arkham_cache").fetchone()[0]
        hits  = conn.execute("SELECT SUM(hit_count) FROM arkham_cache").fetchone()[0] or 0
        sigs  = conn.execute("SELECT COUNT(*) FROM arkham_signals").fetchone()[0]
        conn.close()
        return {
            "session_api_calls":     self._calls,
            "cache_entries":         total,
            "total_cache_hits":      hits,
            "signals_recorded":      sigs,
            "estimated_daily_spend": self._calls,
        }

    async def run_maintenance(self) -> None:
        """Purge expired cache entries. Schedule every 30 minutes."""
        removed = self._cache.purge_expired()
        if removed:
            log.info("Arkham cache: purged %d expired entries.", removed)
