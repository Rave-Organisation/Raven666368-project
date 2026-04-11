"""
Alpha Engine — Telegram Alert System
=======================================
Sends real-time trade notifications directly to your Telegram chat.

Alert types
-----------
  🟢 BUY SIGNAL        — new trade opened
  🔴 SELL / CLOSE      — position closed with PnL
  ⚡ NEW LAUNCH        — high-conviction token detected
  🚨 CIRCUIT BREAKER   — drawdown limit hit, trading halted
  ⚠️  WARNING          — RPC issue, signal silence, wallet drift
  📊 DAILY SUMMARY     — EOD performance report
  💓 HEARTBEAT         — bot is alive confirmation every 6 hours
  ✅ BOT STARTED        — startup confirmation
  🛑 BOT STOPPED        — shutdown confirmation
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any

import aiohttp

from engine.infrastructure.logger import get_logger

log = get_logger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramAlerts:

    def __init__(self, bot_token: str, chat_id: str) -> None:
        self._token   = bot_token
        self._chat_id = chat_id
        self._queue:  asyncio.Queue = asyncio.Queue()
        self._running = False

    @classmethod
    def from_env(cls) -> "TelegramAlerts":
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")
        if not token or not chat_id:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in Replit Secrets."
            )
        return cls(token, chat_id)

    async def start(self) -> None:
        self._running = True
        asyncio.create_task(self._process_queue())
        log.info("TelegramAlerts: sender started.")

    async def stop(self) -> None:
        self._running = False

    async def send_bot_started(self, capital_sol: float, dry_run: bool) -> None:
        mode = "🧪 DRY RUN" if dry_run else "🔴 LIVE TRADING"
        msg = (
            f"✅ *Alpha Engine STARTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Mode:     `{mode}`\n"
            f"Capital:  `{capital_sol:.4f} SOL`\n"
            f"Network:  `Solana Mainnet`\n"
            f"Scanner:  `PumpFun + Raydium`\n"
            f"Time:     `{_now()}`\n\n"
            f"Bot is scanning for new launches 👁"
        )
        await self._enqueue(msg)

    async def send_bot_stopped(self, capital_sol: float, total_trades: int) -> None:
        msg = (
            f"🛑 *Alpha Engine STOPPED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Final Capital: `{capital_sol:.4f} SOL`\n"
            f"Total Trades:  `{total_trades}`\n"
            f"Time: `{_now()}`"
        )
        await self._enqueue(msg)

    async def send_new_signal(
        self,
        symbol:    str,
        mint:      str,
        score:     float,
        liquidity: float,
        tags:      list[str],
    ) -> None:
        tag_str = " · ".join(tags[:4]) if tags else "—"
        msg = (
            f"⚡ *NEW SIGNAL DETECTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Token:      `{symbol}`\n"
            f"Mint:       `{mint[:8]}...`\n"
            f"Score:      `{score:.0f}/100`  {_score_bar(score)}\n"
            f"Liquidity:  `{liquidity:.1f} SOL`\n"
            f"Tags:       `{tag_str}`\n"
            f"Time:       `{_now()}`"
        )
        await self._enqueue(msg)

    async def send_buy_signal(
        self,
        symbol:    str,
        mint:      str,
        score:     float,
        size_sol:  float,
        entry:     float,
        tp_pct:    float,
        sl_pct:    float,
        tx_sig:    str | None = None,
    ) -> None:
        sig_str = f"`{tx_sig[:16]}...`" if tx_sig and tx_sig != "DRY_RUN" else "`DRY RUN`"
        msg = (
            f"🟢 *BUY EXECUTED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Token:     `{symbol}` — `{mint[:8]}...`\n"
            f"Score:     `{score:.0f}/100`  {_score_bar(score)}\n"
            f"Size:      `{size_sol:.4f} SOL`\n"
            f"Entry:     `${entry:.8f}`\n"
            f"Take Profit: `+{tp_pct:.0f}%`\n"
            f"Stop Loss:   `-{sl_pct:.0f}%`\n"
            f"Tx:        {sig_str}\n"
            f"Time:      `{_now()}`"
        )
        await self._enqueue(msg)

    async def send_trade_close(
        self,
        symbol:      str,
        mint:        str,
        pnl_sol:     float,
        exit_reason: str,
        capital_sol: float,
        tx_sig:      str | None = None,
    ) -> None:
        icon    = "🟢" if pnl_sol > 0 else "🔴"
        pnl_str = f"+{pnl_sol:.4f}" if pnl_sol > 0 else f"{pnl_sol:.4f}"
        reason_map = {
            "tp_hit":      "✅ Take Profit Hit",
            "sl_hit":      "❌ Stop Loss Hit",
            "timeout":     "⏱ Timeout Exit",
            "cancelled":   "🛑 Manual Close",
            "end_of_data": "📋 Session End",
        }
        reason_str = reason_map.get(exit_reason, exit_reason)
        sig_str    = f"`{tx_sig[:16]}...`" if tx_sig and tx_sig != "DRY_RUN" else "`DRY RUN`"

        msg = (
            f"{icon} *POSITION CLOSED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Token:    `{symbol}` — `{mint[:8]}...`\n"
            f"PnL:      `{pnl_str} SOL`\n"
            f"Reason:   {reason_str}\n"
            f"Capital:  `{capital_sol:.4f} SOL`\n"
            f"Tx:       {sig_str}\n"
            f"Time:     `{_now()}`"
        )
        await self._enqueue(msg)

    async def send_circuit_breaker(
        self,
        drawdown_pct: float,
        capital_sol:  float,
    ) -> None:
        msg = (
            f"🚨 *CIRCUIT BREAKER TRIGGERED*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Drawdown:  `{drawdown_pct:.1f}%`\n"
            f"Capital:   `{capital_sol:.4f} SOL`\n"
            f"Status:    *ALL TRADING HALTED*\n"
            f"Action:    Manual review required\n"
            f"Time:      `{_now()}`\n\n"
            f"⚠️ No new positions will be opened until you restart the bot."
        )
        await self._enqueue(msg, priority=True)

    async def send_warning(self, title: str, detail: str) -> None:
        msg = (
            f"⚠️ *WARNING — {title}*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{detail}\n"
            f"Time: `{_now()}`"
        )
        await self._enqueue(msg)

    async def send_heartbeat(
        self,
        capital_sol:    float,
        open_positions: int,
        trades_today:   int,
        pnl_today:      float,
        uptime_hours:   float,
    ) -> None:
        pnl_icon = "📈" if pnl_today >= 0 else "📉"
        msg = (
            f"💓 *HEARTBEAT*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"Capital:    `{capital_sol:.4f} SOL`\n"
            f"Open Pos:   `{open_positions}`\n"
            f"Trades:     `{trades_today}` today\n"
            f"{pnl_icon} PnL Today: `{pnl_today:+.4f} SOL`\n"
            f"Uptime:     `{uptime_hours:.1f}h`\n"
            f"Time:       `{_now()}`\n\n"
            f"Bot is running normally ✅"
        )
        await self._enqueue(msg)

    async def send_daily_summary(
        self,
        capital_start: float,
        capital_end:   float,
        total_trades:  int,
        wins:          int,
        losses:        int,
        best_trade:    float,
        worst_trade:   float,
        sharpe:        float,
    ) -> None:
        pnl      = capital_end - capital_start
        roi      = pnl / capital_start * 100 if capital_start else 0
        win_rate = wins / total_trades * 100 if total_trades else 0
        pnl_icon = "📈" if pnl >= 0 else "📉"

        msg = (
            f"📊 *DAILY SUMMARY*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"{pnl_icon} PnL:        `{pnl:+.4f} SOL` (`{roi:+.1f}%`)\n"
            f"Capital:     `{capital_start:.4f}` → `{capital_end:.4f} SOL`\n"
            f"Trades:      `{total_trades}` (W:`{wins}` L:`{losses}`)\n"
            f"Win Rate:    `{win_rate:.1f}%`\n"
            f"Best Trade:  `{best_trade:+.4f} SOL`\n"
            f"Worst Trade: `{worst_trade:+.4f} SOL`\n"
            f"Sharpe:      `{sharpe:.2f}`\n"
            f"Date:        `{datetime.now(timezone.utc).strftime('%Y-%m-%d')}`"
        )
        await self._enqueue(msg)

    async def send_raw(self, text: str) -> None:
        await self._enqueue(text)

    async def _enqueue(self, text: str, priority: bool = False) -> None:
        await self._queue.put(text)

    async def _process_queue(self) -> None:
        while self._running:
            try:
                msg = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                await self._send(msg)
                self._queue.task_done()
                await asyncio.sleep(0.3)
            except asyncio.TimeoutError:
                continue
            except Exception as exc:
                log.warning("TelegramAlerts queue error: %s", exc)

    async def _send(self, text: str) -> bool:
        url  = TELEGRAM_API.format(token=self._token, method="sendMessage")
        body = {
            "chat_id":    self._chat_id,
            "text":       text,
            "parse_mode": "Markdown",
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body,
                    timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    if resp.status == 429:
                        data = await resp.json()
                        retry = data.get("parameters", {}).get("retry_after", 5)
                        log.warning("Telegram rate limited — waiting %ds", retry)
                        await asyncio.sleep(retry)
                        return await self._send(text)
                    if resp.status != 200:
                        log.warning("Telegram HTTP %d", resp.status)
                        return False
                    return True
        except Exception as exc:
            log.warning("Telegram send error: %s", exc)
            return False


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

def _score_bar(score: float) -> str:
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)
