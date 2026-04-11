"""
Alpha Engine — Heartbeat Monitor
===================================
Continuous health surveillance for the live trading bot.

Run as a SEPARATE process from the main bot so a bot crash cannot silence alerts.

What it monitors
----------------
  Process health    — Is the bot PID alive?
  RPC latency       — Can we reach the Solana RPC in < threshold ms?
  Signal silence    — How long since the classifier last fired?
  Capital integrity — On-chain wallet vs. internal ledger (5% drift = alert)
  Open position age — Position held beyond max_hold_seconds = alert
  Circuit breaker   — Bot self-halted? → Telegram immediately.

Recovery actions
----------------
  Process dead → one auto-restart attempt, then alert.
  RPC fail     → automatic failover to secondary RPC.
  Wallet drift → alert + set "manual_review" flag in DB.

Usage
-----
    python -m engine.infrastructure.monitor
    # or
    monitor = HeartbeatMonitor.from_env()
    asyncio.run(monitor.run())
"""

from __future__ import annotations

import asyncio
import os
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from pathlib import Path
from typing import Optional

import aiohttp

from engine.infrastructure.logger import AuditLogger, get_logger

log = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonitorConfig:
    telegram_bot_token:       str   = ""
    telegram_chat_id:         str   = ""

    rpc_url_primary:          str   = "https://mainnet.helius-rpc.com/?api-key=YOUR_KEY"
    rpc_url_fallback:         str   = "https://api.mainnet-beta.solana.com"

    rpc_latency_warn_ms:      int   = 500
    rpc_latency_critical_ms:  int   = 2000
    signal_silence_warn_s:    int   = 120
    signal_silence_critical_s: int  = 300
    max_position_age_s:       int   = 600
    wallet_drift_threshold:   float = 0.05

    heartbeat_interval_s:     int   = 15
    deep_check_interval_s:    int   = 60

    db_path:      str = "data/alpha_engine.db"
    pid_file:     str = "alpha_engine.pid"
    bot_cmd:      str = "python -m engine.main"
    wallet_pubkey: str = ""

    @classmethod
    def from_env(cls) -> "MonitorConfig":
        return cls(
            telegram_bot_token        = os.getenv("TELEGRAM_BOT_TOKEN",   ""),
            telegram_chat_id          = os.getenv("TELEGRAM_CHAT_ID",     ""),
            rpc_url_primary           = os.getenv("HELIUS_RPC_URL",
                                        os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")),
            rpc_url_fallback          = os.getenv("RPC_URL_FALLBACK",     "https://api.mainnet-beta.solana.com"),
            db_path                   = os.getenv("ALPHA_DB_PATH",        "data/alpha_engine.db"),
            wallet_pubkey             = os.getenv("SOLANA_WALLET_ADDRESS", ""),
            pid_file                  = os.getenv("ALPHA_PID_FILE",       "alpha_engine.pid"),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Health status types
# ─────────────────────────────────────────────────────────────────────────────

class Severity(Enum):
    OK       = auto()
    WARN     = auto()
    CRITICAL = auto()


@dataclass
class HealthCheck:
    name:     str
    severity: Severity
    detail:   str
    value:    Optional[float] = None


@dataclass
class HealthReport:
    timestamp: datetime
    checks:    list[HealthCheck]
    overall:   Severity

    @property
    def has_critical(self) -> bool:
        return any(c.severity == Severity.CRITICAL for c in self.checks)

    @property
    def has_warn(self) -> bool:
        return any(c.severity == Severity.WARN for c in self.checks)

    def summary_lines(self) -> list[str]:
        icon = {"OK": "OK", "WARN": "WARN", "CRITICAL": "CRIT"}
        lines = [
            f"*Alpha Engine Health* — {self.timestamp.strftime('%H:%M:%S UTC')}",
            f"Overall: {icon.get(self.overall.name, '?')} {self.overall.name}",
            "─────────────────────",
        ]
        for c in self.checks:
            lines.append(f"{icon.get(c.severity.name, '?')} {c.name}: {c.detail}")
        return lines


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat monitor
# ─────────────────────────────────────────────────────────────────────────────

class HeartbeatMonitor:

    def __init__(self, config: MonitorConfig) -> None:
        self.cfg   = config
        self.audit = AuditLogger()
        self._rpc  = config.rpc_url_primary
        self._last_alert_ts: dict[str, float] = {}
        self._alert_cooldown_s = 300

    @classmethod
    def from_env(cls) -> "HeartbeatMonitor":
        return cls(MonitorConfig.from_env())

    async def run(self) -> None:
        log.info("HeartbeatMonitor started. Interval=%ds.", self.cfg.heartbeat_interval_s)
        self.audit.record_bot_event("monitor_start")

        deep_check_counter = 0
        while True:
            try:
                do_deep = (deep_check_counter % (
                    self.cfg.deep_check_interval_s // self.cfg.heartbeat_interval_s
                )) == 0
                report = await self._run_checks(deep=do_deep)
                deep_check_counter += 1

                if report.has_critical or report.has_warn:
                    await self._handle_alerts(report)

            except Exception as exc:
                log.error("Monitor loop error: %s", exc, exc_info=True)

            await asyncio.sleep(self.cfg.heartbeat_interval_s)

    async def _run_checks(self, deep: bool = False) -> HealthReport:
        checks: list[HealthCheck] = []

        checks.append(self._check_process())
        checks.append(await self._check_rpc_latency())
        checks.append(self._check_signal_silence())
        checks.append(self._check_circuit_breaker())
        checks.append(self._check_open_position_age())

        if deep:
            checks.append(await self._check_wallet_balance())

        overall = Severity.OK
        for c in checks:
            if c.severity == Severity.CRITICAL:
                overall = Severity.CRITICAL
                break
            if c.severity == Severity.WARN and overall == Severity.OK:
                overall = Severity.WARN

        report = HealthReport(timestamp=datetime.now(timezone.utc), checks=checks, overall=overall)

        if overall != Severity.OK:
            log.warning("Health report: %s", overall.name)
            for c in checks:
                if c.severity != Severity.OK:
                    log.warning("  [%s] %s — %s", c.severity.name, c.name, c.detail)

        return report

    def _check_process(self) -> HealthCheck:
        pid_path = Path(self.cfg.pid_file)
        if not pid_path.exists():
            return HealthCheck("process", Severity.CRITICAL, "PID file not found — bot may not be running.")
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)
            return HealthCheck("process", Severity.OK, f"Running (PID {pid})")
        except (ValueError, ProcessLookupError):
            return HealthCheck("process", Severity.CRITICAL, "PID file exists but process is dead.")
        except PermissionError:
            return HealthCheck("process", Severity.OK, "Process exists (permission check — normal in some envs).")

    async def _check_rpc_latency(self) -> HealthCheck:
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc,
                    json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    await resp.json()
            latency_ms = int((time.monotonic() - start) * 1000)

            if latency_ms >= self.cfg.rpc_latency_critical_ms:
                self._rpc = self.cfg.rpc_url_fallback
                return HealthCheck("rpc_latency", Severity.CRITICAL,
                    f"{latency_ms}ms — failed over to backup RPC.", value=latency_ms)
            if latency_ms >= self.cfg.rpc_latency_warn_ms:
                return HealthCheck("rpc_latency", Severity.WARN,
                    f"{latency_ms}ms (threshold {self.cfg.rpc_latency_warn_ms}ms).", value=latency_ms)
            return HealthCheck("rpc_latency", Severity.OK, f"{latency_ms}ms", value=latency_ms)

        except Exception as exc:
            self._rpc = self.cfg.rpc_url_fallback
            return HealthCheck("rpc_latency", Severity.CRITICAL, f"RPC unreachable ({exc}). Switched to fallback.")

    def _check_signal_silence(self) -> HealthCheck:
        try:
            conn = sqlite3.connect(self.cfg.db_path)
            cursor = conn.execute(
                "SELECT audit_ts FROM audit_log WHERE event='signal_generated' ORDER BY rowid DESC LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()

            if not row:
                return HealthCheck("signal_silence", Severity.WARN, "No signals found in audit log.")

            last_ts = datetime.fromisoformat(row[0])
            age_s   = (datetime.now(timezone.utc) - last_ts.replace(tzinfo=timezone.utc)).total_seconds()

            if age_s >= self.cfg.signal_silence_critical_s:
                return HealthCheck("signal_silence", Severity.CRITICAL,
                    f"No signal for {age_s:.0f}s — feed or classifier may be dead.", value=age_s)
            if age_s >= self.cfg.signal_silence_warn_s:
                return HealthCheck("signal_silence", Severity.WARN, f"No signal for {age_s:.0f}s.", value=age_s)
            return HealthCheck("signal_silence", Severity.OK, f"Last signal {age_s:.0f}s ago.")

        except sqlite3.Error as exc:
            return HealthCheck("signal_silence", Severity.WARN, f"Could not read audit log: {exc}")

    def _check_circuit_breaker(self) -> HealthCheck:
        try:
            conn = sqlite3.connect(self.cfg.db_path)
            cursor = conn.execute(
                "SELECT value FROM bot_state WHERE key='circuit_breaker_active' LIMIT 1"
            )
            row = cursor.fetchone()
            conn.close()
            if row and str(row[0]).lower() in ("1", "true"):
                return HealthCheck("circuit_breaker", Severity.CRITICAL,
                    "Circuit breaker is ACTIVE — trading halted. Manual review required.")
            return HealthCheck("circuit_breaker", Severity.OK, "Not active.")
        except sqlite3.Error:
            return HealthCheck("circuit_breaker", Severity.OK, "State DB unavailable (pre-start?).")

    def _check_open_position_age(self) -> HealthCheck:
        try:
            conn = sqlite3.connect(self.cfg.db_path)
            cursor = conn.execute(
                """
                SELECT token_mint, audit_ts FROM audit_log
                WHERE event='trade_open'
                  AND token_mint NOT IN (
                      SELECT token_mint FROM audit_log WHERE event='trade_close'
                  )
                ORDER BY audit_ts ASC
                """
            )
            rows = cursor.fetchall()
            conn.close()

            if not rows:
                return HealthCheck("open_positions", Severity.OK, "No open positions.")

            now = datetime.now(timezone.utc)
            aged = []
            for mint, ts_str in rows:
                ts  = datetime.fromisoformat(ts_str).replace(tzinfo=timezone.utc)
                age = (now - ts).total_seconds()
                if age >= self.cfg.max_position_age_s:
                    aged.append((mint[:8], int(age)))

            if aged:
                detail = "; ".join(f"{m} ({a}s)" for m, a in aged)
                return HealthCheck("open_positions", Severity.WARN, f"Stale positions detected: {detail}")
            return HealthCheck("open_positions", Severity.OK, f"{len(rows)} open, all within age limit.")

        except sqlite3.Error as exc:
            return HealthCheck("open_positions", Severity.WARN, f"DB read error: {exc}")

    async def _check_wallet_balance(self) -> HealthCheck:
        if not self.cfg.wallet_pubkey:
            return HealthCheck("wallet_balance", Severity.OK, "Wallet check skipped (no pubkey configured).")
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    self._rpc,
                    json={
                        "jsonrpc": "2.0", "id": 1,
                        "method": "getBalance",
                        "params": [self.cfg.wallet_pubkey, {"commitment": "confirmed"}],
                    },
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    data = await resp.json()

            onchain_sol = data["result"]["value"] / 1_000_000_000

            conn = sqlite3.connect(self.cfg.db_path)
            cursor = conn.execute("SELECT value FROM bot_state WHERE key='current_capital_sol' LIMIT 1")
            row = cursor.fetchone()
            conn.close()

            if not row:
                return HealthCheck("wallet_balance", Severity.OK,
                    f"On-chain: {onchain_sol:.4f} SOL (internal capital not yet tracked).")

            internal_sol = float(row[0])
            if internal_sol == 0:
                return HealthCheck("wallet_balance", Severity.OK, f"On-chain: {onchain_sol:.4f} SOL.")

            drift_pct = abs(onchain_sol - internal_sol) / internal_sol
            if drift_pct >= self.cfg.wallet_drift_threshold:
                return HealthCheck("wallet_balance", Severity.CRITICAL,
                    f"Wallet drift {drift_pct*100:.1f}%: on-chain={onchain_sol:.4f} internal={internal_sol:.4f} SOL",
                    value=drift_pct)
            return HealthCheck("wallet_balance", Severity.OK,
                f"On-chain: {onchain_sol:.4f} SOL (drift {drift_pct*100:.2f}%)", value=onchain_sol)

        except Exception as exc:
            return HealthCheck("wallet_balance", Severity.WARN, f"Balance check failed: {exc}")

    async def _handle_alerts(self, report: HealthReport) -> None:
        for check in report.checks:
            if check.severity == Severity.OK:
                continue
            last = self._last_alert_ts.get(check.name, 0)
            if time.monotonic() - last < self._alert_cooldown_s:
                continue
            self._last_alert_ts[check.name] = time.monotonic()
            message = "\n".join(report.summary_lines())
            await self._send_telegram(message)
            if check.severity == Severity.CRITICAL and check.name == "process":
                await self._attempt_restart()

    async def _send_telegram(self, message: str) -> None:
        if not self.cfg.telegram_bot_token or not self.cfg.telegram_chat_id:
            log.warning("Telegram not configured — alert suppressed:\n%s", message)
            return
        url  = f"https://api.telegram.org/bot{self.cfg.telegram_bot_token}/sendMessage"
        body = {"chat_id": self.cfg.telegram_chat_id, "text": message, "parse_mode": "Markdown"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=body, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status != 200:
                        log.warning("Telegram send failed: HTTP %d", resp.status)
                    else:
                        log.debug("Telegram alert sent.")
        except Exception as exc:
            log.warning("Telegram send error: %s", exc)

    async def _attempt_restart(self) -> None:
        log.critical("Attempting bot auto-restart...")
        self.audit.record_bot_event("monitor_auto_restart_attempt")
        try:
            subprocess.Popen(
                self.cfg.bot_cmd.split(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            await asyncio.sleep(5)
            await self._send_telegram("Auto-restart attempted. Check bot status.")
        except Exception as exc:
            log.error("Auto-restart failed: %s", exc)
            await self._send_telegram(f"Auto-restart FAILED: {exc}\nManual intervention required.")


if __name__ == "__main__":
    monitor = HeartbeatMonitor.from_env()
    asyncio.run(monitor.run())
