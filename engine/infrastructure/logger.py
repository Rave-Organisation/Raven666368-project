"""
Alpha Engine — Structured Logger
==================================
JSON-formatted, context-enriched logging with two separate streams:

  1. Application log  — standard rotating file + console (DEBUG → CRITICAL)
  2. Audit trail      — append-only, trade-grade JSON records written to a
                        separate file that feeds the SQLite audit table.

Usage
-----
    from engine.infrastructure.logger import get_logger, AuditLogger

    log = get_logger(__name__)
    log.info("Signal generated", extra={"token": mint, "score": 87.3})

    audit = AuditLogger()
    audit.record_trade_open(...)
    audit.record_trade_close(...)
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ──────────────────────────────────────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────────────────────────────────────

LOG_DIR = Path(os.getenv("ALPHA_LOG_DIR", "logs"))
LOG_DIR.mkdir(parents=True, exist_ok=True)

APP_LOG_FILE   = LOG_DIR / "alpha_engine.log"
AUDIT_LOG_FILE = LOG_DIR / "audit_trail.jsonl"       # newline-delimited JSON

LOG_LEVEL     = os.getenv("ALPHA_LOG_LEVEL", "INFO").upper()
MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB per file
BACKUP_COUNT  = 7                  # 7 rolling files → ~70 MB max


# ──────────────────────────────────────────────────────────────────────────────
# JSON formatter
# ──────────────────────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """
    Formats every log record as a single-line JSON object.
    Any keyword arguments passed via `extra={}` are merged into the payload.
    """

    RESERVED_ATTRS = frozenset({
        "args", "asctime", "created", "exc_info", "exc_text", "filename",
        "funcName", "levelname", "levelno", "lineno", "message", "module",
        "msecs", "msg", "name", "pathname", "process", "processName",
        "relativeCreated", "stack_info", "thread", "threadName",
    })

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts":     datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level":  record.levelname,
            "logger": record.name,
            "msg":    record.getMessage(),
        }

        if record.levelno >= logging.WARNING:
            payload["loc"] = f"{record.pathname}:{record.lineno}"

        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)

        for key, val in record.__dict__.items():
            if key not in self.RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = val

        return json.dumps(payload, default=str)


# ──────────────────────────────────────────────────────────────────────────────
# Logger factory
# ──────────────────────────────────────────────────────────────────────────────

_configured = False


def _configure_root() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    formatter = JsonFormatter()

    file_handler = logging.handlers.RotatingFileHandler(
        APP_LOG_FILE,
        maxBytes=MAX_LOG_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(_DevFormatter())
    console_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    root.addHandler(file_handler)
    root.addHandler(console_handler)


class _DevFormatter(logging.Formatter):
    """Compact, colourised console output for local development."""

    COLOURS = {
        "DEBUG":    "\033[37m",
        "INFO":     "\033[36m",
        "WARNING":  "\033[33m",
        "ERROR":    "\033[31m",
        "CRITICAL": "\033[35m",
    }
    RESET = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        colour  = self.COLOURS.get(record.levelname, "")
        ts      = datetime.fromtimestamp(record.created, tz=timezone.utc).strftime("%H:%M:%S")
        message = record.getMessage()
        loc     = f" [{record.name}]" if record.levelno >= logging.WARNING else ""
        return f"{colour}{ts} {record.levelname:<8}{self.RESET}{loc} {message}"


def get_logger(name: str) -> logging.Logger:
    _configure_root()
    return logging.getLogger(name)


# ──────────────────────────────────────────────────────────────────────────────
# SQLite bridge — tables consumed by HeartbeatMonitor
# ──────────────────────────────────────────────────────────────────────────────

_AUDIT_DB_SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_ts   TEXT    NOT NULL,
    event      TEXT    NOT NULL,
    token_mint TEXT,
    raw_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log (event);
CREATE INDEX IF NOT EXISTS idx_audit_ts    ON audit_log (audit_ts);
CREATE INDEX IF NOT EXISTS idx_audit_mint  ON audit_log (token_mint);

CREATE TABLE IF NOT EXISTS bot_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_DB_PATH: Path | None = None


def _get_db_path() -> Path | None:
    """Resolve the SQLite path from env, lazily."""
    global _DB_PATH
    if _DB_PATH is not None:
        return _DB_PATH
    raw = os.getenv("ALPHA_DB_PATH", "")
    if not raw:
        return None
    _DB_PATH = Path(raw)
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _DB_PATH


def _init_db(db: Path) -> None:
    conn = sqlite3.connect(db)
    conn.executescript(_AUDIT_DB_SCHEMA)
    conn.commit()
    conn.close()


_db_initialized: set[str] = set()


def _write_audit_row(event: str, token_mint: str | None, record: dict[str, Any]) -> None:
    """Write one row to the SQLite audit_log table (silently skipped if no DB configured)."""
    db = _get_db_path()
    if db is None:
        return
    db_key = str(db)
    if db_key not in _db_initialized:
        _init_db(db)
        _db_initialized.add(db_key)
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            "INSERT INTO audit_log (audit_ts, event, token_mint, raw_json) VALUES (?,?,?,?)",
            (record.get("audit_ts", ""), event, token_mint, json.dumps(record, default=str)),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error as exc:
        # Do not propagate — a DB write failure must never crash the bot
        pass


def set_bot_state(key: str, value: Any) -> None:
    """
    Persist a key/value pair to the `bot_state` SQLite table so the monitor
    can read capital and circuit-breaker state without importing the engine.
    """
    db = _get_db_path()
    if db is None:
        return
    db_key = str(db)
    if db_key not in _db_initialized:
        _init_db(db)
        _db_initialized.add(db_key)
    try:
        conn = sqlite3.connect(db)
        conn.execute(
            """
            INSERT INTO bot_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, str(value), datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


# ──────────────────────────────────────────────────────────────────────────────
# Append-only audit trail
# ──────────────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Dual-write audit trail:
      1. Append-only JSONL flat file  — ground truth, never modified
      2. SQLite `audit_log` table     — queried by HeartbeatMonitor in real-time

    The SQLite write is best-effort: a DB failure is silently swallowed so it
    can never crash the bot. The JSONL file is the authoritative source of truth.

    Usage
    -----
        audit = AuditLogger()
        audit.record_trade_open(trade_id, mint, entry, size, sl, tp, score)
        audit.record_trade_close(trade_id, mint, exit_price, pnl, "tp_hit")

        # Set observable bot state for the monitor
        set_bot_state("circuit_breaker_active", False)
        set_bot_state("current_capital_sol", 10.432)
    """

    def __init__(self, path: Path = AUDIT_LOG_FILE) -> None:
        self._path = path

    def _write(self, record: dict[str, Any]) -> None:
        record["audit_ts"] = datetime.now(timezone.utc).isoformat()
        # 1. JSONL
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        # 2. SQLite (best-effort)
        _write_audit_row(
            event      = record.get("event", "unknown"),
            token_mint = record.get("token_mint"),
            record     = record,
        )

    def record_signal(
        self,
        token_mint: str,
        conviction_score: float,
        direction: str,
        source_tags: list[str],
        raw_features: dict[str, Any],
    ) -> None:
        self._write({
            "event":        "signal_generated",
            "token_mint":   token_mint,
            "conviction":   conviction_score,
            "direction":    direction,
            "source_tags":  source_tags,
            "raw_features": raw_features,
        })

    def record_trade_open(
        self,
        trade_id: str,
        token_mint: str,
        entry_price: float,
        size_sol: float,
        stop_loss: float,
        take_profit: float,
        conviction_score: float,
        tx_signature: str | None = None,
    ) -> None:
        self._write({
            "event":         "trade_open",
            "trade_id":      trade_id,
            "token_mint":    token_mint,
            "entry_price":   entry_price,
            "size_sol":      size_sol,
            "stop_loss":     stop_loss,
            "take_profit":   take_profit,
            "conviction":    conviction_score,
            "tx_signature":  tx_signature,
        })

    def record_trade_close(
        self,
        trade_id: str,
        token_mint: str,
        exit_price: float,
        pnl_sol: float,
        exit_reason: str,
        tx_signature: str | None = None,
    ) -> None:
        self._write({
            "event":        "trade_close",
            "trade_id":     trade_id,
            "token_mint":   token_mint,
            "exit_price":   exit_price,
            "pnl_sol":      pnl_sol,
            "exit_reason":  exit_reason,
            "tx_signature": tx_signature,
        })

    def record_circuit_breaker(self, reason: str, portfolio_state: dict[str, Any]) -> None:
        rec = {
            "event":     "circuit_breaker_triggered",
            "reason":    reason,
            "portfolio": portfolio_state,
        }
        self._write(rec)
        set_bot_state("circuit_breaker_active", True)

    def record_bot_event(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self._write({"event": event, **(detail or {})})
