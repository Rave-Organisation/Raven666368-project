"""
Alpha Engine — Telegram Signal Ingestor
=========================================
Watches one or more Telegram channels/groups for contract address signals.
When a Solana CA is detected in any message, it:

  1. Validates the address on-chain via Helius RPC (getTokenSupply)
  2. Runs the ConvictionEngine pre-score
  3. Queues a trade if score >= MIN_CONVICTION

Operates independently from the PumpFun scanner — both can run in parallel.
Use this when your alpha comes from Telegram KOL channels, not PumpFun.

Architecture
------------
  - Async Telethon client in user-bot (account) mode
  - CA extraction via regex
  - On-chain verification via Helius JSON-RPC
  - Emits TradeCandidate objects consumed by the main bot loop

Setup (required)
----------------
  pip install telethon
  Env vars:
    TELEGRAM_API_ID      — from my.telegram.org
    TELEGRAM_API_HASH    — from my.telegram.org
    TELEGRAM_BOT_TOKEN   — from @BotFather (if using bot mode)
    HELIUS_RPC_URL       — mainnet Helius endpoint
    SIGNAL_CHANNELS      — comma-separated channel usernames or IDs
    MIN_CONVICTION       — minimum score to queue a trade (default 65)
    DRY_RUN              — if true, log but don't execute
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Awaitable

import aiohttp

from engine.infrastructure.logger import AuditLogger, get_logger

log   = get_logger(__name__)
audit = AuditLogger()

SOL_CA_PATTERN = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
LAMPORTS_PER_SOL = 1_000_000_000


@dataclass
class TradeCandidate:
    ca:              str
    source_channel:  str
    sender:          str
    raw_text:        str
    on_chain_supply: float | None
    safety_score:    float
    detected_at:     datetime

    @property
    def is_valid(self) -> bool:
        return self.on_chain_supply is not None and self.safety_score >= 50


class TelegramIngestor:
    """
    Telethon-based signal listener.

    Usage
    -----
        ingestor = TelegramIngestor.from_env()
        ingestor.on_candidate = my_async_handler
        await ingestor.run()

    The `on_candidate` callback is called for every CA that passes on-chain
    verification. Wire it to AlphaEngineBot._handle_telegram_signal().
    """

    def __init__(
        self,
        api_id:         int,
        api_hash:       str,
        bot_token:      str | None,
        rpc_url:        str,
        channels:       list[str],
        min_conviction: float = 65.0,
        dry_run:        bool  = True,
        on_candidate:   Callable[[TradeCandidate], Awaitable[None]] | None = None,
    ) -> None:
        self._api_id        = api_id
        self._api_hash      = api_hash
        self._bot_token     = bot_token
        self._rpc_url       = rpc_url
        self._channels      = channels
        self._min_conviction = min_conviction
        self._dry_run       = dry_run
        self.on_candidate   = on_candidate
        self._seen_cas: set[str] = set()

        if dry_run:
            log.warning("TelegramIngestor: DRY RUN — signals logged only, no trades.")

    @classmethod
    def from_env(cls) -> "TelegramIngestor":
        api_id    = int(os.getenv("TELEGRAM_API_ID", "0"))
        api_hash  = os.getenv("TELEGRAM_API_HASH", "")
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
        rpc_url   = os.getenv("HELIUS_RPC_URL") or os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
        channels  = [c.strip() for c in os.getenv("SIGNAL_CHANNELS", "").split(",") if c.strip()]

        if not api_id or not api_hash:
            raise RuntimeError(
                "TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in Secrets. "
                "Get them from https://my.telegram.org"
            )
        if not channels:
            raise RuntimeError(
                "SIGNAL_CHANNELS must be set — comma-separated channel usernames or IDs."
            )

        return cls(
            api_id         = api_id,
            api_hash       = api_hash,
            bot_token      = bot_token,
            rpc_url        = rpc_url,
            channels       = channels,
            min_conviction = float(os.getenv("MIN_CONVICTION", "65")),
            dry_run        = os.getenv("DRY_RUN", "true").lower() == "true",
        )

    async def run(self) -> None:
        try:
            from telethon import TelegramClient, events
        except ImportError:
            raise RuntimeError("Install telethon: pip install telethon")

        session_name = "alpha_engine_session"
        client = TelegramClient(session_name, self._api_id, self._api_hash)

        @client.on(events.NewMessage(chats=self._channels or None))
        async def handle_message(event):
            await self._process_message(
                text    = event.raw_text,
                channel = str(getattr(await event.get_chat(), "username", event.chat_id) or event.chat_id),
                sender  = str(getattr(await event.get_sender(), "username", event.sender_id) or "unknown"),
            )

        if self._bot_token:
            await client.start(bot_token=self._bot_token)
            log.info("TelegramIngestor: started in bot mode.")
        else:
            await client.start()
            log.info("TelegramIngestor: started in user mode.")

        log.info("TelegramIngestor: watching channels: %s", self._channels)
        audit.record_bot_event("telegram_ingestor_start", {"channels": self._channels})
        await client.run_until_disconnected()

    async def _process_message(self, text: str, channel: str, sender: str) -> None:
        matches = SOL_CA_PATTERN.findall(text)
        if not matches:
            return

        log.info("Signal detected from %s by %s — %d CA(s)", channel, sender, len(matches))

        for ca in matches:
            if ca in self._seen_cas:
                log.debug("Skipping duplicate CA %s", ca[:8])
                continue
            self._seen_cas.add(ca)

            candidate = await self._verify_and_score(ca, channel, sender, text)
            if not candidate:
                continue

            log.info(
                "CANDIDATE: %s | supply=%.0f | score=%.1f | channel=%s",
                ca[:8], candidate.on_chain_supply or 0, candidate.safety_score, channel,
            )

            audit.record_signal(
                token_mint       = ca,
                conviction_score = candidate.safety_score,
                direction        = "LONG",
                source_tags      = [f"telegram:{channel}", f"sender:{sender}"],
                raw_features     = {"supply": candidate.on_chain_supply, "text_snippet": text[:200]},
            )

            if self._dry_run:
                log.info("DRY RUN — would queue trade for %s", ca[:8])
                continue

            if candidate.is_valid and candidate.safety_score >= self._min_conviction:
                if self.on_candidate:
                    await self.on_candidate(candidate)

    async def _verify_and_score(
        self,
        ca:      str,
        channel: str,
        sender:  str,
        text:    str,
    ) -> TradeCandidate | None:
        supply = await self._get_token_supply(ca)

        if supply is None:
            log.debug("CA %s not verified on-chain — skipping.", ca[:8])
            return None

        score = self._score(ca, supply, channel, text)

        return TradeCandidate(
            ca              = ca,
            source_channel  = channel,
            sender          = sender,
            raw_text        = text,
            on_chain_supply = supply,
            safety_score    = score,
            detected_at     = datetime.now(timezone.utc),
        )

    async def _get_token_supply(self, ca: str) -> float | None:
        payload = {
            "jsonrpc": "2.0",
            "id":      1,
            "method":  "getTokenSupply",
            "params":  [ca],
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc_url,
                    json    = payload,
                    headers = {"Content-Type": "application/json"},
                    timeout = aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()

            if "error" in data:
                log.debug("getTokenSupply error for %s: %s", ca[:8], data["error"].get("message", ""))
                return None

            ui_amount = data.get("result", {}).get("value", {}).get("uiAmount")
            return float(ui_amount) if ui_amount is not None else None

        except Exception as exc:
            log.debug("RPC error for %s: %s", ca[:8], exc)
            return None

    @staticmethod
    def _score(ca: str, supply: float, channel: str, text: str) -> float:
        score = 50.0

        if supply > 0:
            score += 10
        if supply > 1_000_000:
            score += 5
        if supply > 1_000_000_000:
            score -= 5

        text_lower = text.lower()
        if any(kw in text_lower for kw in ["gem", "buy", "entry", "x10", "100x"]):
            score += 5
        if any(kw in text_lower for kw in ["rug", "scam", "fake"]):
            score -= 20

        score = max(0.0, min(100.0, score))
        return round(score, 1)
