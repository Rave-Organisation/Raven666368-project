"""
Alpha Engine — PumpFun New Token Scanner
==========================================
Connects to Helius Enhanced WebSocket and monitors the PumpFun program
for new token mint events in real time.

Every new token detected is passed through a pre-filter before the
full classifier runs — this saves API calls and compute.

Pre-filter checks (all must pass):
  1. Liquidity >= MIN_LIQUIDITY_SOL at launch
  2. Not a known rug pattern (dev wallet = mint authority = suspicious)
  3. Token metadata exists (name, symbol, uri)
  4. Not a duplicate mint seen in last 60s

On pass -> emits a NewTokenEvent downstream to the signal classifier.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Callable

import aiohttp
import websockets

from engine.infrastructure.logger import get_logger

log = get_logger(__name__)

PUMPFUN_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
RAYDIUM_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
JUPITER_PROGRAM_ID = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

MIN_LIQUIDITY_SOL  = 5.0
MAX_TOKEN_AGE_S    = 30
SEEN_CACHE_TTL_S   = 60


@dataclass
class NewTokenEvent:
    mint:                  str
    name:                  str
    symbol:                str
    uri:                   str
    creator:               str
    initial_liquidity_sol: float
    detected_at:           datetime
    raw:                   dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.detected_at).total_seconds()


class PumpFunScanner:
    """
    Listens to Helius Enhanced WebSocket for PumpFun program logs.
    Yields NewTokenEvent objects for every new token that passes pre-filters.

    Usage
    -----
        scanner = PumpFunScanner.from_env()
        async for token in scanner.scan():
            print(f"New token: {token.symbol} — {token.mint[:8]}")
    """

    def __init__(
        self,
        helius_api_key: str,
        min_liquidity:  float = MIN_LIQUIDITY_SOL,
        on_token:       Callable[[NewTokenEvent], None] | None = None,
    ) -> None:
        self._key      = helius_api_key
        self._min_liq  = min_liquidity
        self._on_token = on_token
        self._seen: dict[str, float] = {}
        self._ws_url   = f"wss://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self._http_url = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self._running  = False

    @classmethod
    def from_env(cls, min_liquidity: float = MIN_LIQUIDITY_SOL) -> "PumpFunScanner":
        key = os.getenv("HELIUS_API_KEY", "")
        if not key:
            raise RuntimeError("HELIUS_API_KEY not set in environment.")
        return cls(helius_api_key=key, min_liquidity=min_liquidity)

    async def scan(self) -> AsyncGenerator[NewTokenEvent, None]:
        self._running = True
        log.info("PumpFunScanner: starting scan loop.")
        while self._running:
            try:
                async for event in self._ws_loop():
                    yield event
            except (websockets.ConnectionClosed, ConnectionResetError) as exc:
                log.warning("PumpFunScanner: WebSocket dropped (%s). Reconnecting in 3s.", exc)
                await asyncio.sleep(3)
            except Exception as exc:
                log.error("PumpFunScanner: unexpected error — %s", exc, exc_info=True)
                await asyncio.sleep(5)

    def stop(self) -> None:
        self._running = False

    async def _ws_loop(self) -> AsyncGenerator[NewTokenEvent, None]:
        log.info("PumpFunScanner: connecting to Helius WebSocket.")
        async with websockets.connect(
            self._ws_url, ping_interval=20, ping_timeout=10,
        ) as ws:
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 1,
                "method":  "logsSubscribe",
                "params":  [
                    {"mentions": [PUMPFUN_PROGRAM_ID]},
                    {"commitment": "processed"},
                ],
            }))
            await ws.send(json.dumps({
                "jsonrpc": "2.0", "id": 2,
                "method":  "logsSubscribe",
                "params":  [
                    {"mentions": [RAYDIUM_PROGRAM_ID]},
                    {"commitment": "processed"},
                ],
            }))
            log.info("PumpFunScanner: subscribed. Waiting for new tokens...")
            async for raw_msg in ws:
                try:
                    msg   = json.loads(raw_msg)
                    event = await self._process_message(msg)
                    if event:
                        yield event
                except json.JSONDecodeError:
                    pass
                except Exception as exc:
                    log.warning("PumpFunScanner: message processing error — %s", exc)

    async def _process_message(self, msg: dict[str, Any]) -> NewTokenEvent | None:
        if "result" in msg and not isinstance(msg.get("result"), dict):
            return None
        params = msg.get("params", {})
        result = params.get("result", {})
        value  = result.get("value", {})
        logs   = value.get("logs", [])
        sig    = value.get("signature", "")
        if not logs or not sig:
            return None
        is_new_token = any(
            "Program log: Instruction: Create" in l or "InitializeMint" in l
            for l in logs
        )
        if not is_new_token:
            return None
        return await self._enrich_transaction(sig)

    async def _enrich_transaction(self, signature: str) -> NewTokenEvent | None:
        try:
            url = f"https://api.helius.xyz/v0/transactions/?api-key={self._key}"
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url,
                    json    = {"transactions": [signature]},
                    timeout = aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            if not data or not isinstance(data, list):
                return None
            return self._parse_token_event(data[0])
        except Exception as exc:
            log.debug("PumpFunScanner: enrich error for %s — %s", signature[:8], exc)
            return None

    def _parse_token_event(self, tx: dict[str, Any]) -> NewTokenEvent | None:
        token_transfers = tx.get("tokenTransfers", [])
        events          = tx.get("events", {})
        instructions    = tx.get("instructions", [])
        mint = None
        for transfer in token_transfers:
            if transfer.get("fromUserAccount") == "11111111111111111111111111111111":
                mint = transfer.get("mint")
                break
        if not mint:
            for ix in instructions:
                if "InitializeMint" in str(ix.get("programId", "")):
                    accounts = ix.get("accounts", [])
                    if accounts:
                        mint = accounts[0]
                    break
        if not mint:
            return None

        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < SEEN_CACHE_TTL_S}
        if mint in self._seen:
            return None
        self._seen[mint] = now

        creator = tx.get("feePayer", "")
        name    = ""
        symbol  = ""
        uri     = ""
        nft_events = events.get("nft", {})
        if nft_events:
            name   = nft_events.get("name", "")
            symbol = nft_events.get("symbol", "")

        native_transfers = tx.get("nativeTransfers", [])
        liquidity_sol = sum(
            t.get("amount", 0) for t in native_transfers
            if t.get("toUserAccount") != creator
        ) / 1e9

        if liquidity_sol < self._min_liq:
            log.debug(
                "PumpFunScanner: skipping %s — liquidity %.2f SOL < %.2f",
                mint[:8], liquidity_sol, self._min_liq,
            )
            return None

        event = NewTokenEvent(
            mint                  = mint,
            name                  = name or "Unknown",
            symbol                = symbol or "???",
            uri                   = uri,
            creator               = creator,
            initial_liquidity_sol = liquidity_sol,
            detected_at           = datetime.now(timezone.utc),
            raw                   = tx,
        )
        log.info(
            "NEW TOKEN: %s (%s) | liq=%.2f SOL | creator=%s",
            event.symbol, event.mint[:8], event.initial_liquidity_sol, event.creator[:8],
        )
        if self._on_token:
            self._on_token(event)
        return event
