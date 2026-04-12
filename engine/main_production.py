"""
Alpha Engine — Production Bot
$10 SOL capital | Helius webhook | Jupiter swaps | Telegram alerts
Optimised for Replit — zero waste.

Run: uvicorn engine.main_production:app --host 0.0.0.0 --port 8080
Webhook URL: https://<your-repl>.replit.app/webhook
"""

import os, json, asyncio, base64, logging
from datetime import datetime, timezone
from typing import Optional

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("alpha")

# ── Config — ALL from Replit Secrets ─────────────────────────────────────────
RPC        = os.getenv("RPC_URL") or os.getenv("HELIUS_RPC_URL", "https://api.mainnet-beta.solana.com")
WALLET_KEY = os.getenv("WALLET_PRIVATE_KEY", "")
TG_TOKEN   = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT    = os.environ["TELEGRAM_CHAT_ID"]
WEBHOOK_PATH = "/webhook"

# ── Risk parameters ───────────────────────────────────────────────────────────
CAPITAL_SOL   = float(os.getenv("CAPITAL_SOL",   "10.0"))
RISK_PCT      = float(os.getenv("RISK_PCT",       "0.05"))
MAX_RISK_PCT  = float(os.getenv("MAX_RISK_PCT",   "0.10"))
TP_PCT        = float(os.getenv("TP_PCT",         "0.20"))
SL_PCT        = float(os.getenv("SL_PCT",         "0.07"))
MAX_POSITIONS = int(os.getenv("MAX_POSITIONS",    "2"))
MIN_LIQ_SOL   = float(os.getenv("MIN_LIQ_SOL",   "5.0"))
CIRCUIT_DD    = float(os.getenv("CIRCUIT_DD",     "0.25"))
DRY_RUN       = os.getenv("DRY_RUN", "true").lower() == "true"

# ── Jupiter endpoints ─────────────────────────────────────────────────────────
JUP_QUOTE = "https://quote-api.jup.ag/v6/quote"
JUP_SWAP  = "https://quote-api.jup.ag/v6/swap"
SOL_MINT  = "So11111111111111111111111111111111111111112"

# ── In-memory state ───────────────────────────────────────────────────────────
positions:    dict  = {}
capital_sol:  float = CAPITAL_SOL
peak_capital: float = CAPITAL_SOL
daily_pnl:    float = 0.0
trades_today: int   = 0
halted:       bool  = False

app = FastAPI(docs_url=None, redoc_url=None)

# ── Keypair (lazy — only used in LIVE mode) ───────────────────────────────────
_kp = None

def _keypair():
    global _kp
    if _kp:
        return _kp
    if not WALLET_KEY:
        raise RuntimeError("WALLET_PRIVATE_KEY not set — required for LIVE mode.")
    try:
        import base58
        from solders.keypair import Keypair
        _kp = Keypair.from_bytes(base58.b58decode(WALLET_KEY))
        return _kp
    except ImportError:
        raise RuntimeError("Install solders and base58: pip install solders base58")

def _pubkey() -> str:
    if DRY_RUN and not WALLET_KEY:
        return "DRY_RUN_WALLET"
    return str(_keypair().pubkey())

PUBKEY = _pubkey()
log.info("Wallet: %s | DRY_RUN=%s", PUBKEY[:12], DRY_RUN)

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception as e:
        log.warning("TG send failed: %s", e)

# ── Capital helpers ───────────────────────────────────────────────────────────
def available_sol() -> float:
    locked = sum(p["entry_sol"] for p in positions.values())
    return max(0.0, capital_sol - locked)

def position_size(conviction: float = 1.0) -> float:
    pct  = min(RISK_PCT * conviction, MAX_RISK_PCT)
    size = round(capital_sol * pct, 4)
    return min(size, available_sol() * 0.95)

def drawdown() -> float:
    return (peak_capital - capital_sol) / peak_capital if peak_capital > 0 else 0.0

def regime() -> str:
    if drawdown() > 0.15:
        return "bear"
    if daily_pnl > capital_sol * 0.05:
        return "bull"
    return "chop"

# ── On-chain balance (30s cache) ──────────────────────────────────────────────
_balance_cache: tuple[float, float] = (0.0, 0.0)

def get_balance() -> float:
    global _balance_cache
    if DRY_RUN and not WALLET_KEY:
        return capital_sol
    now = datetime.now(timezone.utc).timestamp()
    if now - _balance_cache[1] < 30:
        return _balance_cache[0]
    try:
        r = requests.post(RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "getBalance",
            "params":  [PUBKEY, {"commitment": "confirmed"}]
        }, timeout=5).json()
        sol = r["result"]["value"] / 1e9
        _balance_cache = (sol, now)
        return sol
    except Exception as e:
        log.warning("Balance fetch failed: %s", e)
        return _balance_cache[0]

# ── Jupiter quote & swap ──────────────────────────────────────────────────────
def jupiter_quote(sol_amount: float, out_mint: str, slippage_bps: int = 200) -> Optional[dict]:
    try:
        r = requests.get(JUP_QUOTE, params={
            "inputMint":   SOL_MINT,
            "outputMint":  out_mint,
            "amount":      int(sol_amount * 1e9),
            "slippageBps": slippage_bps,
        }, timeout=6)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning("Jupiter quote failed: %s", e)
        return None

def jupiter_sell_quote(mint: str, token_amount: int, slippage_bps: int = 300) -> Optional[dict]:
    try:
        r = requests.get(JUP_QUOTE, params={
            "inputMint":   mint,
            "outputMint":  SOL_MINT,
            "amount":      token_amount,
            "slippageBps": slippage_bps,
        }, timeout=6)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.warning("Jupiter sell quote failed: %s", e)
        return None

def execute_swap(quote: dict) -> Optional[str]:
    if DRY_RUN:
        log.info("DRY RUN — swap not sent")
        return "DRY_RUN"
    try:
        from solders.transaction import VersionedTransaction
        kp = _keypair()
        swap_resp = requests.post(JUP_SWAP, json={
            "quoteResponse":             quote,
            "userPublicKey":             PUBKEY,
            "wrapAndUnwrapSol":          True,
            "dynamicComputeUnitLimit":   True,
            "prioritizationFeeLamports": 25_000,
        }, timeout=10).json()

        tx_b64 = swap_resp.get("swapTransaction")
        if not tx_b64:
            log.error("No swapTransaction in response")
            return None

        tx = VersionedTransaction.from_bytes(base64.b64decode(tx_b64))
        tx.sign([kp])

        sig_resp = requests.post(RPC, json={
            "jsonrpc": "2.0", "id": 1,
            "method":  "sendTransaction",
            "params":  [
                base64.b64encode(bytes(tx)).decode(),
                {"encoding": "base64", "preflightCommitment": "processed"},
            ],
        }, timeout=15).json()

        sig = sig_resp.get("result")
        if sig:
            log.info("Swap sent: %s", sig[:20])
        else:
            log.error("Swap failed: %s", sig_resp.get("error"))
        return sig

    except Exception as e:
        log.error("execute_swap error: %s", e)
        return None

# ── Token price via Jupiter ───────────────────────────────────────────────────
def get_token_price_in_sol(mint: str) -> Optional[float]:
    try:
        r = requests.get(
            f"https://price.jup.ag/v4/price?ids={mint}&vsToken={SOL_MINT}",
            timeout=4,
        ).json()
        return r["data"][mint]["price"]
    except Exception:
        return None

# ── Core trade logic ──────────────────────────────────────────────────────────
async def enter_trade(mint: str, liquidity_sol: float, conviction: float = 1.0) -> None:
    global capital_sol, peak_capital, trades_today

    if halted:
        log.info("Halted — skipping %s", mint[:8])
        return
    if len(positions) >= MAX_POSITIONS:
        log.info("Max positions (%d) — skipping %s", MAX_POSITIONS, mint[:8])
        return
    if mint in positions:
        return
    if liquidity_sol < MIN_LIQ_SOL:
        log.info("Low liquidity %.1f SOL — skipping %s", liquidity_sol, mint[:8])
        return

    size = position_size(conviction)
    if size < 0.05:
        log.warning("Position size too small (%.4f SOL) — skipping", size)
        return

    log.info("ENTER %s | size=%.4f SOL | conviction=%.1f", mint[:8], size, conviction)

    quote = jupiter_quote(size, mint)
    if not quote:
        return

    price_impact = float(quote.get("priceImpactPct", 0)) * 100
    if price_impact > 3.0:
        log.warning("Price impact %.1f%% too high — skipping %s", price_impact, mint[:8])
        tg(f"⚠️ Skipped `{mint[:8]}` — price impact {price_impact:.1f}%")
        return

    sig = execute_swap(quote)
    if not sig:
        return

    out_amt     = int(quote.get("outAmount", 0))
    entry_price = size / out_amt if out_amt > 0 else 0

    positions[mint] = {
        "entry_sol":   size,
        "entry_price": entry_price,
        "out_amount":  out_amt,
        "tp_sol":      size * (1 + TP_PCT),
        "sl_sol":      size * (1 - SL_PCT),
        "entered_at":  datetime.now(timezone.utc).isoformat(),
        "sig":         sig,
    }

    capital_sol  -= size
    trades_today += 1

    mode = "🧪 DRY" if DRY_RUN else "✅ LIVE"
    tg(
        f"🟢 *BUY* `{mint[:8]}...`\n"
        f"Size: `{size:.4f} SOL`\n"
        f"TP: `+{TP_PCT*100:.0f}%` | SL: `-{SL_PCT*100:.0f}%`\n"
        f"Impact: `{price_impact:.2f}%` | {mode}"
    )

    asyncio.create_task(monitor_position(mint))


async def monitor_position(mint: str) -> None:
    TIMEOUT_S = int(os.getenv("TRADE_TIMEOUT_S", "300"))
    entered   = datetime.now(timezone.utc)

    while mint in positions:
        await asyncio.sleep(5)
        pos = positions.get(mint)
        if not pos:
            break

        age_s = (datetime.now(timezone.utc) - entered).total_seconds()
        price = get_token_price_in_sol(mint)

        if price is None:
            continue

        current_value = pos["out_amount"] * price if pos["out_amount"] else pos["entry_sol"]

        hit_tp      = current_value >= pos["tp_sol"]
        hit_sl      = current_value <= pos["sl_sol"]
        hit_timeout = age_s >= TIMEOUT_S

        if hit_tp or hit_sl or hit_timeout:
            reason = "TP" if hit_tp else "SL" if hit_sl else "TIMEOUT"
            await close_position(mint, current_value, reason)
            break


async def close_position(mint: str, current_value_sol: float, reason: str) -> None:
    global capital_sol, peak_capital, daily_pnl, halted

    pos = positions.pop(mint, None)
    if not pos:
        return

    quote = jupiter_sell_quote(mint, pos["out_amount"])
    sig   = execute_swap(quote) if quote else None

    pnl         = current_value_sol - pos["entry_sol"]
    capital_sol += pos["entry_sol"] + pnl
    daily_pnl   += pnl
    peak_capital = max(peak_capital, capital_sol)

    icon = "🟢" if pnl > 0 else "🔴"
    log.info("CLOSE %s | pnl=%+.4f SOL | reason=%s | cap=%.4f", mint[:8], pnl, reason, capital_sol)

    tg(
        f"{icon} *CLOSED* `{mint[:8]}...`\n"
        f"PnL: `{pnl:+.4f} SOL`\n"
        f"Reason: `{reason}`\n"
        f"Capital: `{capital_sol:.4f} SOL`"
    )

    if drawdown() >= CIRCUIT_DD:
        halted = True
        tg(f"🚨 *CIRCUIT BREAKER* — drawdown {drawdown()*100:.1f}%\nAll trading halted.")


# ── Helius Enhanced Transactions webhook ──────────────────────────────────────
@app.post(WEBHOOK_PATH)
async def helius_webhook(req: Request):
    """
    Helius Enhanced Transactions webhook.
    Set your webhook URL in the Helius dashboard to:
      https://<your-repl>.replit.app/webhook
    """
    try:
        events = await req.json()
        if not isinstance(events, list):
            events = [events]

        for event in events:
            token_mint = None
            liquidity  = 0.0

            for transfer in event.get("tokenTransfers", []):
                if transfer.get("fromUserAccount") == "11111111111111111111111111111111":
                    token_mint = transfer.get("mint")
                    break

            for t in event.get("nativeTransfers", []):
                liquidity += t.get("amount", 0) / 1e9

            if token_mint and token_mint not in positions:
                log.info("Webhook: new token %s liq=%.2f SOL", token_mint[:8], liquidity)
                asyncio.create_task(enter_trade(token_mint, liquidity))

        return JSONResponse({"ok": True})

    except Exception as e:
        log.error("Webhook error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Status endpoints ──────────────────────────────────────────────────────────
@app.get("/")
async def status():
    bal = get_balance()
    return {
        "status":         "halted" if halted else ("dry_run" if DRY_RUN else "scanning"),
        "dry_run":        DRY_RUN,
        "capital_sol":    round(capital_sol, 4),
        "on_chain_sol":   round(bal, 4),
        "drawdown_pct":   round(drawdown() * 100, 2),
        "regime":         regime(),
        "open_positions": len(positions),
        "trades_today":   trades_today,
        "daily_pnl":      round(daily_pnl, 4),
        "positions": {
            k: {"entry_sol": v["entry_sol"], "tp_pct": TP_PCT * 100, "sl_pct": SL_PCT * 100}
            for k, v in positions.items()
        },
    }

@app.get("/health")
async def health():
    return {"ok": True, "ts": datetime.now(timezone.utc).isoformat()}


# ── Telegram command polling ──────────────────────────────────────────────────
_tg_offset = 0

async def tg_poll_loop():
    global _tg_offset, halted, capital_sol
    log.info("Telegram command listener started.")
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _tg_offset, "timeout": 10, "limit": 5},
                timeout=15,
            ).json()
            for update in r.get("result", []):
                _tg_offset = update["update_id"] + 1
                msg  = update.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()

                if chat != str(TG_CHAT):
                    continue

                if text == "/status":
                    bal = get_balance()
                    tg(
                        f"📊 *Status*\n"
                        f"Capital: `{capital_sol:.4f} SOL`\n"
                        f"On-chain: `{bal:.4f} SOL`\n"
                        f"Open: `{len(positions)}`\n"
                        f"PnL today: `{daily_pnl:+.4f} SOL`\n"
                        f"DD: `{drawdown()*100:.1f}%`\n"
                        f"Mode: `{'HALTED' if halted else 'DRY RUN' if DRY_RUN else 'LIVE'}`"
                    )
                elif text == "/pause":
                    halted = True
                    tg("⏸ *Paused.* Send /resume to restart.")
                elif text == "/resume":
                    halted = False
                    tg("▶️ *Resumed.* Bot is scanning.")
                elif text == "/positions":
                    if not positions:
                        tg("No open positions.")
                    else:
                        lines = ["📋 *Open Positions*"]
                        for m, p in positions.items():
                            lines.append(f"`{m[:8]}` — `{p['entry_sol']:.4f} SOL`")
                        tg("\n".join(lines))
                elif text in ("/help", "/start"):
                    tg(
                        "📋 *Alpha Engine Commands*\n"
                        "/status — live capital & positions\n"
                        "/positions — open trades\n"
                        "/pause — stop new entries\n"
                        "/resume — restart scanning\n"
                        "/help — this message"
                    )

        except Exception as e:
            log.warning("TG poll error: %s", e)
        await asyncio.sleep(2)


# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    asyncio.create_task(tg_poll_loop())
    mode = "🧪 DRY RUN" if DRY_RUN else "🔴 LIVE"
    tg(
        f"✅ *Alpha Engine ONLINE*\n"
        f"Capital: `{CAPITAL_SOL} SOL`\n"
        f"Risk/trade: `{RISK_PCT*100:.0f}%` → `{CAPITAL_SOL*RISK_PCT:.3f} SOL`\n"
        f"TP: `+{TP_PCT*100:.0f}%` | SL: `-{SL_PCT*100:.0f}%`\n"
        f"Max positions: `{MAX_POSITIONS}`\n"
        f"Min liquidity: `{MIN_LIQ_SOL} SOL`\n"
        f"Circuit breaker: `{CIRCUIT_DD*100:.0f}% drawdown`\n"
        f"Webhook: `{WEBHOOK_PATH}`\n"
        f"Mode: {mode}"
    )
    log.info("Bot started. DRY_RUN=%s CAPITAL=%.2f SOL", DRY_RUN, CAPITAL_SOL)
