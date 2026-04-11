"""
Alpha Engine — Main Bot Entry Point
=====================================
Orchestrates all components into a single autonomous trading loop:

  PumpFunScanner  -> detects new token launches via Helius WebSocket
       |
  Pre-filter      -> liquidity, age, rug pattern checks
       |
  ArkhamOSINT     -> whale/smart money enrichment (only if pre-score >= 50)
       |
  ConvictionEngine -> computes final score 0-100
       |
  RiskEngine      -> sizes the position, checks circuit breakers
       |
  JupiterExecutor -> submits buy via Jupiter V6 + Helius RPC
       |
  PositionManager -> monitors open positions for SL/TP/timeout
       |
  JupiterExecutor -> submits sell on exit signal
       |
  AuditLogger     -> records everything to SQLite + JSONL

Start/stop via Telegram commands or Replit Run button.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from datetime import datetime, timezone

import aiohttp

from engine.execution.arkham_osint     import ArkhamOSINT
from engine.execution.jupiter_executor import JupiterSwapExecutor
from engine.execution.pumpfun_scanner  import NewTokenEvent, PumpFunScanner
from engine.infrastructure.logger      import AuditLogger, get_logger, set_bot_state

log   = get_logger(__name__)
audit = AuditLogger()


class BotConfig:
    INITIAL_CAPITAL_SOL       = float(os.getenv("INITIAL_CAPITAL_SOL",  "10.0"))
    BASE_RISK_PCT             = float(os.getenv("BASE_RISK_PCT",        "0.01"))
    CONVICTION_PUNT_PCT       = float(os.getenv("CONVICTION_PUNT_PCT",  "0.05"))
    CONVICTION_PUNT_THRESHOLD = float(os.getenv("CONVICTION_THRESHOLD", "90.0"))
    STOP_LOSS_PCT             = float(os.getenv("STOP_LOSS_PCT",        "0.05"))
    TAKE_PROFIT_PCT           = float(os.getenv("TAKE_PROFIT_PCT",      "0.15"))
    MAX_OPEN_POSITIONS        = int(os.getenv("MAX_OPEN_POSITIONS",     "3"))
    CIRCUIT_BREAKER_DD_PCT    = float(os.getenv("CIRCUIT_BREAKER_PCT",  "30.0"))
    TRADE_TIMEOUT_S           = int(os.getenv("TRADE_TIMEOUT_S",        "300"))
    MIN_CONVICTION_TO_TRADE   = float(os.getenv("MIN_CONVICTION",       "65.0"))
    DRY_RUN                   = os.getenv("DRY_RUN", "false").lower() == "true"
    MIN_LIQUIDITY_SOL         = float(os.getenv("MIN_LIQUIDITY_SOL",    "5.0"))


class ConvictionEngine:
    """
    Computes a conviction score from raw token signals.

    Score bands
    -----------
      0-50  : No trade
      50-65 : Monitor only
      65-80 : Base risk (1%)
      80-90 : Elevated risk
      90-100: Conviction punt (5%)
    """

    def score(self, event: NewTokenEvent, arkham_boost: float = 0.0) -> tuple[float, list[str]]:
        score = 0.0
        tags: list[str] = []

        liq = event.initial_liquidity_sol
        if liq >= 50:
            score += 25; tags.append("liq_50+SOL")
        elif liq >= 20:
            score += 18; tags.append("liq_20+SOL")
        elif liq >= 10:
            score += 12; tags.append("liq_10+SOL")
        elif liq >= 5:
            score += 6;  tags.append("liq_5+SOL")

        age = event.age_seconds
        if age < 5:
            score += 20; tags.append("ultra_fresh_<5s")
        elif age < 15:
            score += 15; tags.append("fresh_<15s")
        elif age < 30:
            score += 8;  tags.append("fresh_<30s")

        if event.name and event.name != "Unknown" and len(event.name) > 2:
            score += 5; tags.append("has_name")
        if event.symbol and event.symbol != "???" and len(event.symbol) <= 6:
            score += 5; tags.append("has_symbol")
        if event.uri:
            score += 5; tags.append("has_uri")

        if event.creator == event.mint:
            score -= 30; tags.append("RUG_RISK:creator_eq_mint")
        if liq < 3:
            score -= 20; tags.append("LOW_LIQ_WARNING")

        if arkham_boost > 0:
            score += arkham_boost
            tags.append(f"arkham_boost+{arkham_boost:.0f}")

        score = max(0.0, min(100.0, score))
        return round(score, 1), tags


class OpenPosition:
    def __init__(
        self,
        mint:        str,
        entry_price: float,
        size_sol:    float,
        stop_loss:   float,
        take_profit: float,
        entry_time:  datetime,
        conviction:  float,
    ) -> None:
        self.mint        = mint
        self.entry_price = entry_price
        self.size_sol    = size_sol
        self.stop_loss   = stop_loss
        self.take_profit = take_profit
        self.entry_time  = entry_time
        self.conviction  = conviction

    @property
    def age_seconds(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds()

    def should_exit(self, current_price: float, timeout_s: int) -> tuple[bool, str]:
        if current_price <= self.stop_loss:
            return True, "sl_hit"
        if current_price >= self.take_profit:
            return True, "tp_hit"
        if self.age_seconds >= timeout_s:
            return True, "timeout"
        return False, ""


class AlphaEngineBot:

    def __init__(self) -> None:
        cfg = BotConfig()
        self.cfg             = cfg
        self.capital_sol     = cfg.INITIAL_CAPITAL_SOL
        self.peak_capital    = cfg.INITIAL_CAPITAL_SOL
        self.open_positions: list[OpenPosition] = []
        self.daily_loss_sol  = 0.0
        self.circuit_breaker = False
        self.trades_today    = 0
        self._running        = False

        self.scanner    = PumpFunScanner.from_env(min_liquidity=cfg.MIN_LIQUIDITY_SOL)
        self.conviction = ConvictionEngine()
        self.executor   = JupiterSwapExecutor.from_env(dry_run=cfg.DRY_RUN)

        try:
            self.arkham = ArkhamOSINT.from_env()
            log.info("Arkham OSINT: connected.")
        except RuntimeError:
            self.arkham = None
            log.warning("Arkham OSINT: API key not set — running without whale intelligence.")

        if cfg.DRY_RUN:
            log.warning("DRY RUN MODE — no real transactions")

    async def start(self) -> None:
        self._running = True
        set_bot_state("circuit_breaker_active", False)
        set_bot_state("current_capital_sol", self.capital_sol)
        audit.record_bot_event("bot_start", {
            "capital_sol": self.capital_sol,
            "dry_run":     self.cfg.DRY_RUN,
        })
        log.info("Alpha Engine Bot STARTED")
        log.info("Capital: %.4f SOL | DryRun: %s", self.capital_sol, self.cfg.DRY_RUN)

        loop = asyncio.get_event_loop()
        for sig_name in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig_name, lambda: asyncio.create_task(self.stop()))

        await asyncio.gather(
            self._scan_loop(),
            self._position_monitor_loop(),
            self._maintenance_loop(),
        )

    async def stop(self) -> None:
        log.info("Bot shutting down — closing open positions...")
        self._running = False
        self.scanner.stop()
        for pos in list(self.open_positions):
            log.warning("Force-closing position: %s", pos.mint[:8])
            await self.executor.sell_all(pos.mint, slippage_bps=300)
        audit.record_bot_event("bot_stop", {"capital_sol": self.capital_sol})
        log.info("Alpha Engine Bot STOPPED")

    async def _scan_loop(self) -> None:
        async for token_event in self.scanner.scan():
            if not self._running:
                break
            await self._handle_new_token(token_event)

    async def _handle_new_token(self, event: NewTokenEvent) -> None:
        if self.circuit_breaker:
            log.warning("Circuit breaker active — skipping %s", event.symbol)
            return
        if len(self.open_positions) >= self.cfg.MAX_OPEN_POSITIONS:
            log.debug("Max positions reached — skipping %s", event.symbol)
            return

        pre_score, pre_tags = self.conviction.score(event)

        arkham_boost = 0.0
        if self.arkham:
            try:
                enrichment   = await self.arkham.enrich_token(event.mint, pre_score=pre_score)
                arkham_boost = enrichment.total_boost
                pre_tags    += enrichment.signals
            except Exception as exc:
                log.warning("Arkham enrichment failed: %s", exc)

        final_score, final_tags = self.conviction.score(event, arkham_boost)

        log.info(
            "SCORE %s (%s) = %.1f | tags=%s",
            event.symbol, event.mint[:8], final_score, ", ".join(final_tags[:4]),
        )

        audit.record_signal(
            token_mint       = event.mint,
            conviction_score = final_score,
            direction        = "LONG",
            source_tags      = final_tags,
            raw_features     = {
                "liquidity_sol": event.initial_liquidity_sol,
                "age_s":         event.age_seconds,
                "creator":       event.creator,
            },
        )

        if final_score < self.cfg.MIN_CONVICTION_TO_TRADE:
            log.debug("Score %.1f below threshold %.1f — no trade.", final_score, self.cfg.MIN_CONVICTION_TO_TRADE)
            return

        await self._enter_trade(event, final_score, final_tags)

    async def _enter_trade(
        self,
        event: NewTokenEvent,
        score: float,
        tags:  list[str],
    ) -> None:
        if score >= self.cfg.CONVICTION_PUNT_THRESHOLD:
            risk_pct = self.cfg.CONVICTION_PUNT_PCT
        else:
            risk_pct = self.cfg.BASE_RISK_PCT

        size_sol = round(self.capital_sol * risk_pct, 4)
        size_sol = min(size_sol, self.capital_sol * 0.95)

        if size_sol < 0.01:
            log.warning("Position size too small (%.4f SOL) — skipping.", size_sol)
            return

        log.info(
            "ENTER %s | score=%.1f | size=%.4f SOL | risk=%.0f%%",
            event.symbol, score, size_sol, risk_pct * 100,
        )

        result = await self.executor.buy(event.mint, size_sol)
        if not result.success:
            log.error("Buy failed for %s: %s", event.symbol, result.error)
            return

        entry_price = 1.0
        sl_price = entry_price * (1 - self.cfg.STOP_LOSS_PCT)
        tp_price = entry_price * (1 + self.cfg.TAKE_PROFIT_PCT)

        pos = OpenPosition(
            mint        = event.mint,
            entry_price = entry_price,
            size_sol    = size_sol,
            stop_loss   = sl_price,
            take_profit = tp_price,
            entry_time  = datetime.now(timezone.utc),
            conviction  = score,
        )
        self.open_positions.append(pos)
        self.capital_sol -= size_sol
        set_bot_state("current_capital_sol", self.capital_sol)

        audit.record_trade_open(
            trade_id         = result.signature or "unknown",
            token_mint       = event.mint,
            entry_price      = entry_price,
            size_sol         = size_sol,
            stop_loss        = sl_price,
            take_profit      = tp_price,
            conviction_score = score,
            tx_signature     = result.signature,
        )

        log.info(
            "OPEN  %s | sig=%s | SL=%.2f%% TP=%.2f%%",
            event.symbol,
            (result.signature or "DRY")[:16],
            self.cfg.STOP_LOSS_PCT * 100,
            self.cfg.TAKE_PROFIT_PCT * 100,
        )

    async def _position_monitor_loop(self) -> None:
        while self._running:
            await asyncio.sleep(2)
            for pos in list(self.open_positions):
                try:
                    current_price = await self._get_current_price(pos.mint)
                    if current_price is None:
                        continue
                    should_exit, reason = pos.should_exit(current_price, self.cfg.TRADE_TIMEOUT_S)
                    if should_exit:
                        await self._exit_trade(pos, reason, current_price)
                except Exception as exc:
                    log.warning("Position monitor error for %s: %s", pos.mint[:8], exc)

    async def _exit_trade(self, pos: OpenPosition, reason: str, current_price: float) -> None:
        log.info("EXIT %s | reason=%s | age=%.0fs", pos.mint[:8], reason, pos.age_seconds)

        result = await self.executor.sell_all(pos.mint, slippage_bps=200)

        pct_move = (current_price - pos.entry_price) / pos.entry_price
        pnl_sol  = pct_move * pos.size_sol

        self.capital_sol   += pos.size_sol + pnl_sol
        self.peak_capital   = max(self.peak_capital, self.capital_sol)
        self.open_positions = [p for p in self.open_positions if p.mint != pos.mint]

        set_bot_state("current_capital_sol", self.capital_sol)

        if pnl_sol < 0:
            self.daily_loss_sol += pnl_sol

        dd_pct = (self.peak_capital - self.capital_sol) / self.peak_capital * 100
        if dd_pct >= self.cfg.CIRCUIT_BREAKER_DD_PCT:
            self.circuit_breaker = True
            log.critical("CIRCUIT BREAKER TRIGGERED — drawdown %.1f%%", dd_pct)
            audit.record_circuit_breaker("drawdown", {"capital": self.capital_sol, "dd_pct": dd_pct})

        audit.record_trade_close(
            trade_id     = pos.mint[:8],
            token_mint   = pos.mint,
            exit_price   = current_price,
            pnl_sol      = pnl_sol,
            exit_reason  = reason,
            tx_signature = result.signature,
        )

        icon = "+" if pnl_sol > 0 else "-"
        log.info(
            "%s CLOSE %s | pnl=%+.4f SOL | capital=%.4f SOL",
            icon, pos.mint[:8], pnl_sol, self.capital_sol,
        )

    async def _get_current_price(self, token_mint: str) -> float | None:
        try:
            url = f"https://price.jup.ag/v4/price?ids={token_mint}&vsToken={SOL_MINT}"
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=3)) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
            price = data.get("data", {}).get(token_mint, {}).get("price")
            return float(price) if price else None
        except Exception:
            return None

    async def _maintenance_loop(self) -> None:
        while self._running:
            await asyncio.sleep(1800)
            if self.arkham:
                await self.arkham.run_maintenance()
            log.info(
                "HEARTBEAT | capital=%.4f SOL | open=%d | dd=%.1f%%",
                self.capital_sol,
                len(self.open_positions),
                (self.peak_capital - self.capital_sol) / self.peak_capital * 100,
            )


SOL_MINT = "So11111111111111111111111111111111111111112"


async def main() -> None:
    bot = AlphaEngineBot()
    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
