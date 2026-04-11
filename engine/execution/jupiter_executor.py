"""
Alpha Engine — Jupiter Swap Executor
=======================================
Executes buy and sell swaps via Jupiter Aggregator V6 API.
Uses Helius RPC for transaction submission and confirmation.

Flow for every trade
--------------------
  BUY:
    1. Get quote  — Jupiter /quote?inputMint=SOL&outputMint=TOKEN
    2. Get swap tx — Jupiter /swap (returns a versioned transaction)
    3. Add ComputeBudget instructions (priority fee from oracle)
    4. Sign with wallet keypair
    5. Submit via Helius sendTransaction (or Jito bundle)
    6. Poll for confirmation
    7. Record result to audit log

  SELL:
    Same flow with inputMint=TOKEN, outputMint=SOL

Safety guards
-------------
  - Never spend more than MAX_SINGLE_TRADE_SOL in one transaction
  - Slippage capped at MAX_SLIPPAGE_BPS
  - Simulate before sending (catches insufficient funds / bad accounts)
  - Auto-retry once on "blockhash not found" errors
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

import aiohttp

from engine.infrastructure.logger import AuditLogger, get_logger

log = get_logger(__name__)

JUPITER_API_BASE     = "https://quote-api.jup.ag/v6"
SOL_MINT             = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL     = 1_000_000_000
MAX_SLIPPAGE_BPS     = 300
MAX_SINGLE_TRADE_SOL = 2.0
DEFAULT_PRIORITY_FEE = 100_000


@dataclass
class SwapQuote:
    input_mint:   str
    output_mint:  str
    in_amount:    int
    out_amount:   int
    price_impact: float
    slippage_bps: int
    route_plan:   list[str]
    raw:          dict[str, Any]


@dataclass
class SwapResult:
    success:       bool
    signature:     str | None
    in_amount_sol: float
    out_amount:    float
    price_impact:  float
    fee_sol:       float
    error:         str | None = None
    confirmed:     bool = False


class JupiterSwapExecutor:
    """
    Executes token swaps via Jupiter V6 using Helius RPC.

    Parameters
    ----------
    helius_api_key  : Your Helius API key.
    wallet_keypair  : solders Keypair object.
    priority_fee_uL : Compute unit price in micro-lamports.
    dry_run         : If True, builds and simulates the tx but never sends.
    """

    def __init__(
        self,
        helius_api_key:  str,
        wallet_keypair:  Any,
        priority_fee_uL: int  = DEFAULT_PRIORITY_FEE,
        dry_run:         bool = False,
    ) -> None:
        self._key          = helius_api_key
        self._keypair      = wallet_keypair
        self._priority_fee = priority_fee_uL
        self._dry_run      = dry_run
        self._rpc_url      = f"https://mainnet.helius-rpc.com/?api-key={helius_api_key}"
        self._audit        = AuditLogger()
        if dry_run:
            log.warning("JupiterSwapExecutor: DRY RUN mode — no transactions will be sent.")

    @classmethod
    def from_env(cls, dry_run: bool = False) -> "JupiterSwapExecutor":
        helius_key = os.getenv("HELIUS_API_KEY", "")
        if not helius_key:
            raise RuntimeError("HELIUS_API_KEY not set.")
        keypair = cls._load_keypair()
        return cls(
            helius_api_key = helius_key,
            wallet_keypair = keypair,
            dry_run        = dry_run,
        )

    @staticmethod
    def _load_keypair() -> Any:
        try:
            from solders.keypair import Keypair
        except ImportError:
            raise RuntimeError("Install solders: pip install solders")

        key_b58   = os.getenv("WALLET_PRIVATE_KEY", "")
        key_array = os.getenv("WALLET_KEY_ARRAY",   "")

        if key_b58:
            import base58 as b58
            secret = b58.b58decode(key_b58)
            return Keypair.from_bytes(secret)
        if key_array:
            secret = bytes(json.loads(key_array))
            return Keypair.from_bytes(secret)
        raise RuntimeError(
            "No wallet key found. Set WALLET_PRIVATE_KEY or WALLET_KEY_ARRAY in Secrets."
        )

    async def buy(
        self,
        token_mint:   str,
        sol_amount:   float,
        slippage_bps: int = 100,
    ) -> SwapResult:
        if sol_amount > MAX_SINGLE_TRADE_SOL:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=sol_amount, out_amount=0,
                price_impact=0, fee_sol=0,
                error=f"Trade size {sol_amount} SOL exceeds max {MAX_SINGLE_TRADE_SOL} SOL",
            )
        in_lamports = int(sol_amount * LAMPORTS_PER_SOL)
        log.info("BUY  %s | %.4f SOL | slippage=%dbps", token_mint[:8], sol_amount, slippage_bps)
        return await self._execute_swap(SOL_MINT, token_mint, in_lamports, slippage_bps)

    async def sell(
        self,
        token_mint:   str,
        token_amount: int,
        slippage_bps: int = 150,
    ) -> SwapResult:
        log.info("SELL %s | amount=%d | slippage=%dbps", token_mint[:8], token_amount, slippage_bps)
        return await self._execute_swap(token_mint, SOL_MINT, token_amount, slippage_bps)

    async def sell_all(self, token_mint: str, slippage_bps: int = 200) -> SwapResult:
        balance = await self._get_token_balance(token_mint)
        if balance == 0:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=0, out_amount=0,
                price_impact=0, fee_sol=0,
                error="Zero token balance — nothing to sell.",
            )
        return await self.sell(token_mint, balance, slippage_bps)

    async def _execute_swap(
        self,
        input_mint:   str,
        output_mint:  str,
        amount:       int,
        slippage_bps: int,
    ) -> SwapResult:
        quote = await self._get_quote(input_mint, output_mint, amount, slippage_bps)
        if not quote:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=0, price_impact=0, fee_sol=0,
                error="Failed to get Jupiter quote.",
            )
        if quote.price_impact > 5.0:
            log.warning("Swap rejected — price impact %.2f%% exceeds 5%%.", quote.price_impact)
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=0, price_impact=quote.price_impact, fee_sol=0,
                error=f"Price impact too high: {quote.price_impact:.2f}%",
            )
        if quote.slippage_bps > MAX_SLIPPAGE_BPS:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=0, price_impact=quote.price_impact, fee_sol=0,
                error=f"Slippage {quote.slippage_bps}bps > max {MAX_SLIPPAGE_BPS}bps",
            )

        swap_tx_b64 = await self._get_swap_transaction(quote)
        if not swap_tx_b64:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=0, price_impact=quote.price_impact, fee_sol=0,
                error="Failed to build swap transaction.",
            )

        if self._dry_run:
            log.info("DRY RUN — swap tx built successfully, not sending.")
            return SwapResult(
                success=True, signature="DRY_RUN",
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=quote.out_amount,
                price_impact=quote.price_impact,
                fee_sol=0.000025,
                confirmed=False,
            )

        sig = await self._sign_and_send(swap_tx_b64)
        if not sig:
            return SwapResult(
                success=False, signature=None,
                in_amount_sol=amount / LAMPORTS_PER_SOL,
                out_amount=0, price_impact=quote.price_impact, fee_sol=0,
                error="Transaction submission failed.",
            )

        confirmed = await self._confirm_transaction(sig)
        in_sol  = amount / LAMPORTS_PER_SOL if input_mint == SOL_MINT else 0.0
        out_sol = quote.out_amount / LAMPORTS_PER_SOL if output_mint == SOL_MINT else quote.out_amount

        result = SwapResult(
            success       = confirmed,
            signature     = sig,
            in_amount_sol = in_sol,
            out_amount    = out_sol,
            price_impact  = quote.price_impact,
            fee_sol       = 0.000025,
            confirmed     = confirmed,
        )
        direction = "BUY" if output_mint != SOL_MINT else "SELL"
        log.info(
            "%s CONFIRMED=%s | sig=%s | impact=%.2f%%",
            direction, confirmed, sig[:16], quote.price_impact,
        )
        return result

    async def _get_quote(
        self,
        input_mint:   str,
        output_mint:  str,
        amount:       int,
        slippage_bps: int,
    ) -> SwapQuote | None:
        params = {
            "inputMint":           input_mint,
            "outputMint":          output_mint,
            "amount":              str(amount),
            "slippageBps":         str(slippage_bps),
            "onlyDirectRoutes":    "false",
            "asLegacyTransaction": "false",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{JUPITER_API_BASE}/quote",
                    params  = params,
                    timeout = aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status != 200:
                        log.warning("Jupiter quote HTTP %d", resp.status)
                        return None
                    data = await resp.json()
            route_plan = [
                step.get("swapInfo", {}).get("ammKey", "")[:8]
                for step in data.get("routePlan", [])
            ]
            return SwapQuote(
                input_mint   = input_mint,
                output_mint  = output_mint,
                in_amount    = int(data.get("inAmount", amount)),
                out_amount   = int(data.get("outAmount", 0)),
                price_impact = float(data.get("priceImpactPct", 0)) * 100,
                slippage_bps = slippage_bps,
                route_plan   = route_plan,
                raw          = data,
            )
        except Exception as exc:
            log.warning("Jupiter quote error: %s", exc)
            return None

    async def _get_swap_transaction(self, quote: SwapQuote) -> str | None:
        wallet_address = str(self._keypair.pubkey())
        body = {
            "quoteResponse":             quote.raw,
            "userPublicKey":             wallet_address,
            "wrapAndUnwrapSol":          True,
            "useSharedAccounts":         True,
            "prioritizationFeeLamports": self._priority_fee,
            "asLegacyTransaction":       False,
            "useTokenLedger":            False,
            "dynamicComputeUnitLimit":   True,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{JUPITER_API_BASE}/swap",
                    json    = body,
                    timeout = aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status != 200:
                        text = await resp.text()
                        log.warning("Jupiter swap HTTP %d: %s", resp.status, text[:200])
                        return None
                    data = await resp.json()
            return data.get("swapTransaction")
        except Exception as exc:
            log.warning("Jupiter swap error: %s", exc)
            return None

    async def _sign_and_send(self, tx_b64: str, retry: int = 1) -> str | None:
        try:
            from solders.transaction import VersionedTransaction

            tx_bytes = base64.b64decode(tx_b64)
            tx       = VersionedTransaction.from_bytes(tx_bytes)
            tx.sign([self._keypair])
            signed_b64 = base64.b64encode(bytes(tx)).decode()

            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method":  "sendTransaction",
                "params":  [
                    signed_b64,
                    {
                        "encoding":            "base64",
                        "preflightCommitment": "processed",
                        "skipPreflight":       False,
                        "maxRetries":          3,
                    },
                ],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    data = await resp.json()

            if "error" in data:
                err_msg = data["error"].get("message", str(data["error"]))
                if retry > 0 and "Blockhash not found" in err_msg:
                    log.warning("Blockhash expired — retrying with fresh blockhash.")
                    await asyncio.sleep(0.5)
                    fresh_tx = await self._refresh_blockhash(tx)
                    if fresh_tx:
                        fresh_b64 = base64.b64encode(bytes(fresh_tx)).decode()
                        return await self._sign_and_send(fresh_b64, retry=0)
                log.error("sendTransaction error: %s", err_msg)
                return None
            return data.get("result")
        except Exception as exc:
            log.error("Sign and send error: %s", exc, exc_info=True)
            return None

    async def _refresh_blockhash(self, tx: Any) -> Any | None:
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method":  "getLatestBlockhash",
                "params":  [{"commitment": "finalized"}],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
            blockhash = data["result"]["value"]["blockhash"]
            from solders.hash import Hash
            tx.message.recent_blockhash = Hash.from_string(blockhash)
            tx.sign([self._keypair])
            return tx
        except Exception as exc:
            log.warning("refresh_blockhash error: %s", exc)
            return None

    async def _confirm_transaction(
        self,
        signature:  str,
        timeout_s:  int   = 30,
        interval_s: float = 0.5,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        payload  = {
            "jsonrpc": "2.0", "id": 1,
            "method":  "getSignatureStatuses",
            "params":  [[signature], {"searchTransactionHistory": True}],
        }
        while time.monotonic() < deadline:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                        self._rpc_url, json=payload,
                        timeout=aiohttp.ClientTimeout(total=5),
                    ) as resp:
                        data = await resp.json()
                statuses = data.get("result", {}).get("value", [None])
                status   = statuses[0] if statuses else None
                if status:
                    err = status.get("err")
                    if err:
                        log.error("Transaction failed on-chain: %s", err)
                        return False
                    conf = status.get("confirmationStatus", "")
                    if conf in ("confirmed", "finalized"):
                        return True
            except Exception:
                pass
            await asyncio.sleep(interval_s)
        log.warning("Transaction confirmation timeout for %s", signature[:16])
        return False

    async def _get_token_balance(self, token_mint: str) -> int:
        wallet = str(self._keypair.pubkey())
        try:
            payload = {
                "jsonrpc": "2.0", "id": 1,
                "method":  "getTokenAccountsByOwner",
                "params":  [
                    wallet,
                    {"mint": token_mint},
                    {"encoding": "jsonParsed"},
                ],
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc_url, json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()
            accounts = data.get("result", {}).get("value", [])
            if not accounts:
                return 0
            balance_str = (
                accounts[0]
                .get("account", {})
                .get("data", {})
                .get("parsed", {})
                .get("info", {})
                .get("tokenAmount", {})
                .get("amount", "0")
            )
            return int(balance_str)
        except Exception as exc:
            log.warning("get_token_balance error: %s", exc)
            return 0
