"""
Trade Logger
─────────────
CSV-level trade log for quick devnet/mainnet comparison.
JSON audit trail is handled by engine.infrastructure.logger.AuditLogger.
"""
import csv
import os
import time

from engine.infrastructure.logger import get_logger, AuditLogger

log   = get_logger(__name__)
audit = AuditLogger()

LOG_DIR      = "logs"
DEVNET_LOG   = os.path.join(LOG_DIR, "devnet_trades.csv")
MAINNET_LOG  = os.path.join(LOG_DIR, "mainnet_trades.csv")

FIELDNAMES = [
    "timestamp", "network", "mint", "action",
    "price_sol", "amount_sol", "pnl_sol",
    "truth_score", "trim_pct", "efficacy_score", "reason"
]


def _ensure_log(path: str):
    os.makedirs(LOG_DIR, exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDNAMES).writeheader()


def log_trade(
    network: str,
    mint: str,
    action: str,
    price_sol: float,
    amount_sol: float = 0.0,
    pnl_sol: float = 0.0,
    truth_score: float = 0.0,
    trim_pct: float = 0.0,
    efficacy_score: float = 1.0,
    reason: str = ""
):
    log_path = DEVNET_LOG if network == "devnet" else MAINNET_LOG
    _ensure_log(log_path)

    row = {
        "timestamp":      time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "network":        network,
        "mint":           mint,
        "action":         action,
        "price_sol":      round(price_sol, 8),
        "amount_sol":     round(amount_sol, 8),
        "pnl_sol":        round(pnl_sol, 8),
        "truth_score":    round(truth_score, 4),
        "trim_pct":       round(trim_pct, 4),
        "efficacy_score": round(efficacy_score, 4),
        "reason":         reason,
    }

    with open(log_path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=FIELDNAMES).writerow(row)

    log.info("[%s] %s %s @ %.6f SOL | PnL: %+.4f", network.upper(), action, mint, price_sol, pnl_sol)

    audit.record_bot_event("csv_trade_logged", {
        "network": network, "mint": mint, "action": action, "price_sol": price_sol, "pnl_sol": pnl_sol
    })


def read_log(network: str) -> list:
    log_path = DEVNET_LOG if network == "devnet" else MAINNET_LOG
    if not os.path.exists(log_path):
        return []
    with open(log_path, "r") as f:
        return list(csv.DictReader(f))


def compare_devnet_vs_mainnet():
    devnet  = read_log("devnet")
    mainnet = read_log("mainnet")

    log.info("DEVNET:  %d trades logged", len(devnet))
    log.info("MAINNET: %d trades logged", len(mainnet))

    for label, rows in [("DEVNET", devnet), ("MAINNET", mainnet)]:
        if not rows:
            continue
        sells     = [r for r in rows if r["action"] in ("TRIM", "FULL_EXIT", "SELL")]
        total_pnl = sum(float(r["pnl_sol"]) for r in sells)
        log.info("%s: %d exits | Total PnL: %+.4f SOL", label, len(sells), total_pnl)
