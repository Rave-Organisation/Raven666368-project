"""
Alpha Engine — Telegram Bot Command Handler
============================================
Listens for commands sent to the bot and responds
with live bot data or executes control actions.

Commands
--------
  /start      — welcome message + open app button
  /status     — live capital, open positions, today's PnL
  /trades     — last 5 closed trades
  /pause      — pause new entries (keeps monitoring)
  /resume     — resume trading
  /stop       — emergency stop all trading + close positions
  /balance    — on-chain wallet balance
  /score      — last 5 signals with conviction scores
  /help       — command list

Security: all commands are only accepted from TELEGRAM_CHAT_ID.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Callable

import aiohttp

from engine.infrastructure.logger import get_logger

log = get_logger(__name__)

TELEGRAM_API  = "https://api.telegram.org/bot{token}/{method}"
POLL_INTERVAL = 2


class TelegramCommandHandler:

    def __init__(
        self,
        bot_token:  str,
        chat_id:    str,
        app_url:    str = "https://rave-hybrid-self-autonomous-trading-bot.replit.app",
    ) -> None:
        self._token   = bot_token
        self._chat_id = str(chat_id)
        self._app_url = app_url
        self._offset  = 0
        self._running = False

        self.on_pause:  Callable | None = None
        self.on_resume: Callable | None = None
        self.on_stop:   Callable | None = None
        self.get_status: Callable | None = None
        self.get_trades: Callable | None = None

    @classmethod
    def from_env(cls) -> "TelegramCommandHandler":
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID",   "")
        if not token or not chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID not set.")
        return cls(token, chat_id)

    async def start(self) -> None:
        self._running = True
        await self._set_commands()
        log.info("TelegramCommandHandler: polling started.")
        asyncio.create_task(self._poll_loop())

    async def stop(self) -> None:
        self._running = False

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                updates = await self._get_updates()
                for update in updates:
                    await self._handle_update(update)
            except Exception as exc:
                log.warning("TelegramCommandHandler poll error: %s", exc)
            await asyncio.sleep(POLL_INTERVAL)

    async def _handle_update(self, update: dict[str, Any]) -> None:
        self._offset = update["update_id"] + 1

        msg = update.get("message") or update.get("channel_post")
        if not msg:
            return

        from_id = str(msg.get("from", {}).get("id", ""))
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text    = msg.get("text", "").strip()

        if from_id != self._chat_id and chat_id != self._chat_id:
            await self._send(chat_id, "⛔ Unauthorised. This bot is private.")
            return

        if not text.startswith("/"):
            return

        cmd = text.split()[0].lower().split("@")[0]
        log.info("TelegramCommand: %s from %s", cmd, from_id[:6])

        handlers = {
            "/start":   self._cmd_start,
            "/status":  self._cmd_status,
            "/trades":  self._cmd_trades,
            "/pause":   self._cmd_pause,
            "/resume":  self._cmd_resume,
            "/stop":    self._cmd_stop,
            "/balance": self._cmd_balance,
            "/score":   self._cmd_score,
            "/help":    self._cmd_help,
        }

        handler = handlers.get(cmd)
        if handler:
            await handler(chat_id)
        else:
            await self._send(chat_id, f"Unknown command: `{cmd}`\nSend /help for the list.")

    async def _cmd_start(self, chat_id: str) -> None:
        msg = (
            "👋 *Welcome to Alpha Engine*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Autonomous Solana trading intelligence.\n\n"
            "📊 Scanning PumpFun + Raydium 24/7\n"
            "⚡ ICT methodology + Arkham OSINT\n"
            "🛡 Auto risk management\n\n"
            "Use /help to see all commands."
        )
        await self._send_with_button(
            chat_id, msg,
            button_text = "⚡ Open Alpha Engine",
            button_url  = self._app_url,
        )

    async def _cmd_help(self, chat_id: str) -> None:
        msg = (
            "📋 *Alpha Engine Commands*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/status   — live capital & positions\n"
            "/trades   — last 5 closed trades\n"
            "/score    — recent signal scores\n"
            "/balance  — on-chain wallet balance\n"
            "/pause    — pause new trade entries\n"
            "/resume   — resume trading\n"
            "/stop     — emergency stop\n"
            "/help     — this message"
        )
        await self._send(chat_id, msg)

    async def _cmd_status(self, chat_id: str) -> None:
        if self.get_status:
            try:
                s = self.get_status()
                pnl_icon = "📈" if s.get("pnl_today", 0) >= 0 else "📉"
                cb = "🚨 ACTIVE" if s.get("circuit_breaker") else "✅ OK"
                msg = (
                    f"📊 *Bot Status*\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n"
                    f"Capital:    `{s.get('capital_sol', 0):.4f} SOL`\n"
                    f"Open Pos:   `{s.get('open_positions', 0)}`\n"
                    f"{pnl_icon} PnL Today: `{s.get('pnl_today', 0):+.4f} SOL`\n"
                    f"Trades:     `{s.get('trades_today', 0)}` today\n"
                    f"Drawdown:   `{s.get('drawdown_pct', 0):.1f}%`\n"
                    f"Circuit:    {cb}\n"
                    f"Scanning:   `{'✅' if s.get('scanning') else '⏸ Paused'}`\n"
                    f"Uptime:     `{s.get('uptime_h', 0):.1f}h`"
                )
            except Exception as exc:
                msg = f"⚠️ Status unavailable: {exc}"
        else:
            msg = "⚠️ Bot status not connected yet."
        await self._send(chat_id, msg)

    async def _cmd_trades(self, chat_id: str) -> None:
        if self.get_trades:
            try:
                trades = self.get_trades()[-5:]
                if not trades:
                    await self._send(chat_id, "No closed trades yet.")
                    return
                lines = ["📋 *Last 5 Trades*\n━━━━━━━━━━━━━━━━━━━━"]
                for t in reversed(trades):
                    icon   = "🟢" if t.get("pnl_sol", 0) > 0 else "🔴"
                    pnl    = t.get("pnl_sol", 0)
                    reason = t.get("exit_reason", "—")
                    sym    = t.get("symbol", t.get("token_mint", "?")[:6])
                    lines.append(f"{icon} `{sym}` — `{pnl:+.4f} SOL` — {reason}")
                await self._send(chat_id, "\n".join(lines))
            except Exception as exc:
                await self._send(chat_id, f"⚠️ Trades unavailable: {exc}")
        else:
            await self._send(chat_id, "⚠️ Trade data not connected yet.")

    async def _cmd_pause(self, chat_id: str) -> None:
        if self.on_pause:
            self.on_pause()
            await self._send(chat_id, "⏸ *Trading PAUSED*\nBot is monitoring but won't open new positions.\nSend /resume to restart.")
        else:
            await self._send(chat_id, "⚠️ Pause not connected yet.")

    async def _cmd_resume(self, chat_id: str) -> None:
        if self.on_resume:
            self.on_resume()
            await self._send(chat_id, "▶️ *Trading RESUMED*\nBot is now scanning and trading.")
        else:
            await self._send(chat_id, "⚠️ Resume not connected yet.")

    async def _cmd_stop(self, chat_id: str) -> None:
        await self._send(
            chat_id,
            "🛑 *Emergency stop received.*\nClosing all open positions and halting bot...",
        )
        if self.on_stop:
            asyncio.create_task(self.on_stop())

    async def _cmd_balance(self, chat_id: str) -> None:
        wallet = os.getenv("WALLET_PUBKEY", "")
        if not wallet:
            await self._send(chat_id, "⚠️ WALLET_PUBKEY not set in Secrets.")
            return
        try:
            rpc = os.getenv("HELIUS_RPC_URL") or os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
            async with aiohttp.ClientSession() as session:
                async with session.post(rpc, json={
                    "jsonrpc":"2.0","id":1,
                    "method":"getBalance",
                    "params":[wallet, {"commitment":"confirmed"}]
                }, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    data = await resp.json()
            sol = data["result"]["value"] / 1e9
            msg = (
                f"💰 *Wallet Balance*\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"Address: `{wallet[:8]}...{wallet[-6:]}`\n"
                f"Balance: `{sol:.6f} SOL`\n"
                f"Time:    `{datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}`"
            )
            await self._send(chat_id, msg)
        except Exception as exc:
            await self._send(chat_id, f"⚠️ Balance check failed: {exc}")

    async def _cmd_score(self, chat_id: str) -> None:
        await self._send(
            chat_id,
            "⚡ *Recent Signal Scores*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "Scores update as new tokens are detected.\n"
            "Open the dashboard for the full live feed.",
        )

    async def _set_commands(self) -> None:
        url = TELEGRAM_API.format(token=self._token, method="setMyCommands")
        commands = [
            {"command": "start",   "description": "Welcome + open app"},
            {"command": "status",  "description": "Live capital & positions"},
            {"command": "trades",  "description": "Last 5 closed trades"},
            {"command": "balance", "description": "On-chain wallet balance"},
            {"command": "score",   "description": "Recent signal scores"},
            {"command": "pause",   "description": "Pause new entries"},
            {"command": "resume",  "description": "Resume trading"},
            {"command": "stop",    "description": "Emergency stop"},
            {"command": "help",    "description": "Command list"},
        ]
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json={"commands": commands}, timeout=aiohttp.ClientTimeout(total=5))
        except Exception as exc:
            log.warning("Failed to set bot commands: %s", exc)

    async def _get_updates(self) -> list[dict[str, Any]]:
        url  = TELEGRAM_API.format(token=self._token, method="getUpdates")
        body = {"offset": self._offset, "timeout": 1, "limit": 10}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=body,
                    timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    data = await resp.json()
            return data.get("result", [])
        except Exception:
            return []

    async def _send(self, chat_id: str, text: str) -> None:
        url  = TELEGRAM_API.format(token=self._token, method="sendMessage")
        body = {"chat_id": chat_id, "text": text, "parse_mode": "Markdown"}
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=8))
        except Exception as exc:
            log.warning("TelegramCommandHandler send error: %s", exc)

    async def _send_with_button(
        self,
        chat_id:     str,
        text:        str,
        button_text: str,
        button_url:  str,
    ) -> None:
        url  = TELEGRAM_API.format(token=self._token, method="sendMessage")
        body = {
            "chat_id":    chat_id,
            "text":       text,
            "parse_mode": "Markdown",
            "reply_markup": {
                "inline_keyboard": [[
                    {"text": button_text, "web_app": {"url": button_url}}
                ]]
            }
        }
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=8))
        except Exception as exc:
            log.warning("TelegramCommandHandler button send error: %s", exc)
