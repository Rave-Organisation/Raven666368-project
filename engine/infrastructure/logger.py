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
# Append-only audit trail
# ──────────────────────────────────────────────────────────────────────────────

class AuditLogger:
    """
    Write-once JSON-L audit records for every trade lifecycle event.
    These feed both the SQLite audit table and serve as the ground truth
    for the ML training pipeline and verifiable track record.
    """

    def __init__(self, path: Path = AUDIT_LOG_FILE) -> None:
        self._path = path

    def _write(self, record: dict[str, Any]) -> None:
        record["audit_ts"] = datetime.now(timezone.utc).isoformat()
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")

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
        self._write({
            "event":     "circuit_breaker_triggered",
            "reason":    reason,
            "portfolio": portfolio_state,
        })

    def record_bot_event(self, event: str, detail: dict[str, Any] | None = None) -> None:
        self._write({"event": event, **(detail or {})})
