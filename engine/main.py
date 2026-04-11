"""
SOL Trader Bot - Main Compounding Loop
───────────────────────────────────────
Slow-compounding, self-aware Solana bot.
Run on devnet first, then switch to mainnet once validated.

Usage:
    python -m engine.main
"""
import os
import time

from engine.infrastructure.logger import get_logger, AuditLogger
from engine.intelligence import (
    calculate_regime_intelligence,
    end_session_and_learn,
    slow_mode_cap,
    AI,
    MAX_CONCURRENT_TOKENS,
    MAX_RISK_PCT,
)
from engine.trade_logger import log_trade, compare_devnet_vs_mainnet

log   = get_logger(__name__)
audit = AuditLogger()

NETWORK = os.getenv("SOLANA_NETWORK", "devnet")
WALLET  = os.getenv("SOLANA_WALLET_ADDRESS", "YourWalletAddressHere")
MIN_LOOP_SECONDS = 5


# ─── Placeholder Helpers (replace with real Helius/RPC calls) ────────────────
def get_sol_balance(wallet: str) -> float:
    return 10.0  # replace: solana_client.get_balance(...)


def get_position_size_sol(mint: str) -> float:
    return 0.5   # replace: fetch token balance * price


def get_price_sol(mint: str) -> float:
    return 0.001  # replace: fetch from Helius / DexScreener


def ranked_tokens() -> list:
    return ["TOKEN_A", "TOKEN_B"]  # replace: your signal-ranked list


def compute_truth_score(mint: str) -> tuple:
    truth_score   = 0.6   # replace: your Truth-Engine logic
    cex_confirmed = False
    return truth_score, cex_confirmed


def execute_full_sell(mint: str, size_sol: float):
    price = get_price_sol(mint)
    log.info("FULL SELL %s = %.4f SOL @ %.6f", mint, size_sol, price)
    log_trade(NETWORK, mint, "FULL_EXIT", price, amount_sol=size_sol, reason="CEX dump confirmed")
    audit.record_trade_close(
        trade_id    = f"auto_{mint[:8]}",
        token_mint  = mint,
        exit_price  = price,
        pnl_sol     = 0.0,    # replace with real PnL calc
        exit_reason = "cex_dump_confirmed",
    )


def execute_partial_sell(mint: str, amount_sol: float, trim_pct: float, truth_score: float):
    price = get_price_sol(mint)
    log.info("TRIM %.0f%% %s = %.4f SOL @ %.6f", trim_pct * 100, mint, amount_sol, price)
    log_trade(NETWORK, mint, "TRIM", price, amount_sol=amount_sol,
              truth_score=truth_score, trim_pct=trim_pct)
    audit.record_bot_event("trim_executed", {
        "mint": mint, "trim_pct": trim_pct, "truth_score": truth_score
    })


# ─── Main Compounding Cycle ──────────────────────────────────────────────────
def run_compounding_cycle():
    current_balance = get_sol_balance(WALLET)
    total_risk_sol  = 0.0

    global_eff    = AI.get_param("global.efficacy", 1.0)
    effective_cap = slow_mode_cap(global_eff)

    log.info(
        "[%s] Balance: %.4f SOL | Cap: %.0f%% | Efficacy: %.3f",
        NETWORK.upper(), current_balance, effective_cap * 100, global_eff
    )

    active = 0
    for mint in ranked_tokens()[:MAX_CONCURRENT_TOKENS]:
        size_sol = get_position_size_sol(mint)
        price    = get_price_sol(mint)
        total_risk_sol += size_sol

        max_per_token = current_balance * effective_cap
        max_risk      = current_balance * MAX_RISK_PCT

        if size_sol > max_per_token:
            log.debug("%s: position too large (%.4f > %.4f SOL cap). Skipping.", mint, size_sol, max_per_token)
            continue

        if total_risk_sol > max_risk:
            log.warning("Total risk exceeded %.0f%%. Stopping.", MAX_RISK_PCT * 100)
            break

        truth_score, cex_confirmed = compute_truth_score(mint)
        action, pct, is_cex = calculate_regime_intelligence(mint, price, truth_score, cex_confirmed)

        log.info("  %s: truth=%.2f | action=%s | pct=%.2f", mint, truth_score, action, pct)

        if action == "FULL_EXIT" and is_cex:
            execute_full_sell(mint, size_sol)

        elif action == "TRIM" and pct > 0:
            execute_partial_sell(mint, size_sol * pct, pct, truth_score)

        active += 1
        if active >= MAX_CONCURRENT_TOKENS:
            break


# ─── Entry Point ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("SOL Trader Bot starting on %s.", NETWORK.upper())
    log.info("Wallet: %s", WALLET)
    audit.record_bot_event("bot_start", {"network": NETWORK, "wallet": WALLET})

    cycle = 0
    try:
        while True:
            cycle += 1
            log.info("=" * 50 + f" CYCLE {cycle} " + "=" * 50)
            run_compounding_cycle()
            time.sleep(MIN_LOOP_SECONDS)
    except KeyboardInterrupt:
        log.info("Bot stopped by user.")
        audit.record_bot_event("bot_stop", {"cycles": cycle})
        compare_devnet_vs_mainnet()
