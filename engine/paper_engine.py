"""
Alpha Engine — Paper Trading + Historical Backtester
=====================================================
Two validation layers before a single real SOL is spent:

LAYER 1 — Paper Trading (Shadow Mode)
  Listens to the LIVE Helius webhook feed.
  Passes every token through all 17 features in genius_features.py.
  Simulates entries, moonbag exits, SL/TP, trailing stops.
  Logs every decision + outcome to paper_trades.json.
  Sends Telegram alerts identical to live mode — so you experience
  exactly what live trading feels like, zero risk.

LAYER 2 — Historical Backtester
  Replays paper_trades.json + a CSV of past token launches.
  Runs KellyCriterion, SurvivorBiasCorrector, CircadianBias
  against real historical outcomes.
  Produces a final report: win rate, expectancy, max drawdown,
  Sharpe ratio, and Self-Evolution threshold suggestions.

Replit efficiency:
  - Zero database — JSON files only
  - No heavy ML libs — pure math
  - Single asyncio loop — one process
  - Telegram alerts rate-limited to 1/3s
"""

import os, json, asyncio, math, time, logging, csv, tempfile
from collections import defaultdict, deque
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import requests
from fastapi import FastAPI, Request, UploadFile, File, Form, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import JSONResponse

# Import our systems
try:
    try:
        from engine.edge_systems import rug_detector, kelly
        from engine.genius_features import (
            full_pre_trade_gate, moonbag, compounder,
            circadian, evolution, survivor, mtf,
            liq_gravity, social_vel, gas_oracle,
            name_entropy, wash_detect, narrative,
        )
    except ImportError:
        from edge_systems import rug_detector, kelly  # type: ignore
        from genius_features import (  # type: ignore
            full_pre_trade_gate, moonbag, compounder,
            circadian, evolution, survivor, mtf,
            liq_gravity, social_vel, gas_oracle,
            name_entropy, wash_detect, narrative,
        )
    FEATURES_LOADED = True
except ImportError:
    FEATURES_LOADED = False

log = logging.getLogger("alpha.paper")

# ── Config ────────────────────────────────────────────────────────────────────
RPC       = os.environ.get("RPC_URL", "")
TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT   = os.environ.get("TELEGRAM_CHAT_ID", "")
CAPITAL   = float(os.environ.get("CAPITAL_SOL", "10.0"))
TP_PCT    = float(os.environ.get("TP_PCT", "0.20"))
SL_PCT    = float(os.environ.get("SL_PCT", "0.07"))
PAPER_LOG = os.environ.get(
    "PAPER_LOG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "paper_trades.json"),
)
INTERNAL_API_KEY = os.environ.get("INTERNAL_API_KEY", "")

app = FastAPI(docs_url=None, redoc_url=None)

# ── Authentication ─────────────────────────────────────────────────────────────
_bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> None:
    """Dependency that enforces Bearer token authentication on sensitive routes."""
    if not INTERNAL_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="API key not configured on server; set INTERNAL_API_KEY.",
        )
    if credentials is None or credentials.credentials != INTERNAL_API_KEY:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )


# ── Rate limiter ───────────────────────────────────────────────────────────────
# Simple in-memory sliding-window limiter for expensive endpoints.
# Configurable via env vars; defaults to 10 requests per 60 seconds.
_RATE_LIMIT_REQUESTS = int(os.environ.get("HEAVY_RATE_LIMIT_REQUESTS", "10"))
_RATE_LIMIT_WINDOW   = float(os.environ.get("HEAVY_RATE_LIMIT_WINDOW_SEC", "60"))
_rate_windows: dict[str, deque] = defaultdict(deque)


def _heavy_rate_limit(request: Request) -> None:
    """
    Sliding-window rate limit for computationally expensive endpoints.
    Keyed by (route path + client IP) so each endpoint has its own budget.
    """
    key = f"{request.url.path}:{request.client.host if request.client else 'unknown'}"
    now = time.monotonic()
    window = _rate_windows[key]
    cutoff = now - _RATE_LIMIT_WINDOW
    while window and window[0] < cutoff:
        window.popleft()
    if len(window) >= _RATE_LIMIT_REQUESTS:
        raise HTTPException(
            status_code=429,
            detail=(
                f"Too many requests: max {_RATE_LIMIT_REQUESTS} per "
                f"{int(_RATE_LIMIT_WINDOW)}s for this endpoint."
            ),
            headers={"Retry-After": str(int(_RATE_LIMIT_WINDOW))},
        )
    window.append(now)


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADE DATA MODEL
# ══════════════════════════════════════════════════════════════════════════════

@dataclass
class PaperTrade:
    id:            str
    mint:          str
    symbol:        str
    entry_time:    str
    entry_price:   float
    size_sol:      float
    tp_price:      float
    sl_price:      float
    score:         int
    flags:         list

    # Filled on close
    exit_time:     str   = ""
    exit_price:    float = 0.0
    pnl_sol:       float = 0.0
    pnl_pct:       float = 0.0
    exit_reason:   str   = ""
    phase2_held:   bool  = False   # moonbag still running
    phase2_pnl:    float = 0.0

    # Accounting — tracks how much of the original entry remains exposed
    # after any partial (moonbag) exits. Final close PnL is computed only
    # on this remaining exposure to avoid double-counting capital.
    remaining_size_sol: float = 0.0
    partial_pnl_sol:    float = 0.0   # PnL realised by partial exits

    # Feature tracking
    rug_score:     int   = 0
    kelly_pct:     float = 0.0
    circadian_mult: float = 1.0
    wash_ratio:    float = 0.0
    narrative_mult: float = 1.0


# ══════════════════════════════════════════════════════════════════════════════
# PAPER TRADING ENGINE
# ══════════════════════════════════════════════════════════════════════════════

class PaperEngine:
    """
    Runs all 17 features against live token events.
    Never signs a transaction. Everything is simulation.
    """

    def __init__(self):
        self.capital      = CAPITAL
        self.peak         = CAPITAL
        self.open:        dict[str, PaperTrade] = {}
        self.closed:      list[PaperTrade]      = self._load_existing()
        self.detected     = 0    # total tokens seen (for survivor bias)
        self.daily_pnl    = 0.0
        self._prices:     dict[str, list] = defaultdict(list)
        self._tg_last     = 0.0

    # ── Entry ─────────────────────────────────────────────────────────────────

    async def on_new_token(self, mint: str, name: str, symbol: str,
                           creator: str, liquidity_sol: float,
                           transfers: list) -> None:
        self.detected += 1

        if len(self.open) >= int(os.getenv("MAX_POSITIONS", "2")):
            return

        if mint in self.open:
            return

        # Run full 17-feature gate
        ok, size, score, flags = await full_pre_trade_gate(
            mint, name, symbol, creator,
            liquidity_sol, transfers, self.capital,
        ) if FEATURES_LOADED else (True, self.capital * 0.05, 75, [])

        # Rug check
        rug_report = rug_detector.check(mint, creator=creator) if FEATURES_LOADED else None
        rug_score  = rug_report.score if rug_report else 100

        breakdown = self._feature_breakdown(mint, name, symbol, transfers) if FEATURES_LOADED else ""

        if not ok or (rug_report and not rug_report.safe):
            reason = f"BLOCKED score={score} rug={rug_score} flags={flags}"
            log.info("PAPER SKIP %s — %s", mint[:8], reason)
            self._tg(
                f"📋 *PAPER SKIP* `{symbol}`\n"
                f"`{reason}`\n"
                f"{breakdown}"
            )
            evolution.record(mint, "blocked", score, reason) if FEATURES_LOADED else None
            return

        # Kelly sizing
        kelly_result = kelly.compute(
            self.capital,
            [{"pnl_sol": t.pnl_sol, "entry_sol": t.size_sol} for t in self.closed]
        ) if FEATURES_LOADED else None

        size      = kelly_result.position_sol if kelly_result else size
        kelly_pct = kelly_result.half_kelly   if kelly_result else 0.05

        # Entry price — use current Jupiter quote (free)
        entry_price = self._get_price(mint) or 1e-6

        trade = PaperTrade(
            id                 = f"{mint[:6]}_{int(time.time())}",
            mint               = mint,
            symbol             = symbol,
            entry_time         = _now(),
            entry_price        = entry_price,
            size_sol           = size,
            tp_price           = entry_price * (1 + TP_PCT),
            sl_price           = entry_price * (1 - SL_PCT),
            score              = score,
            flags              = flags,
            rug_score          = rug_score,
            kelly_pct          = kelly_pct,
            remaining_size_sol = size,
        )

        self.open[mint]  = trade
        self.capital    -= size

        # Init moonbag tracking
        token_units = int(size / entry_price) if entry_price > 0 else 0
        moonbag.init(mint, size, token_units) if FEATURES_LOADED else None

        # Record to MTF
        mtf.record_price(mint, entry_price) if FEATURES_LOADED else None

        log.info("PAPER ENTER %s score=%d size=%.4f SOL kelly=%.1f%%",
                 symbol, score, size, kelly_pct*100)

        self._tg(
            f"📝 *PAPER BUY* `{symbol}`\n"
            f"Score: `{score}/100`\n"
            f"Size:  `{size:.4f} SOL`\n"
            f"Kelly: `{kelly_pct*100:.1f}%`\n"
            f"Rug:   `{rug_score}/100`\n"
            f"Flags: `{', '.join(flags[:3]) or 'none'}`\n"
            f"{breakdown}"
        )

    # ── Position monitoring ───────────────────────────────────────────────────

    async def monitor_loop(self) -> None:
        """Poll open positions every 5 seconds. Price via Jupiter."""
        while True:
            await asyncio.sleep(5)
            for mint, trade in list(self.open.items()):
                try:
                    price = self._get_price(mint)
                    if not price:
                        continue

                    mtf.record_price(mint, price) if FEATURES_LOADED else None
                    self._prices[mint].append(price)

                    # Moonbag evaluation
                    if FEATURES_LOADED:
                        current_value = trade.size_sol * (price / trade.entry_price)
                        decision = moonbag.evaluate(mint, current_value)

                        if decision["action"] == "partial_sell":
                            self._partial_close(trade, price, "MOONBAG_2X")
                            continue

                        if decision["action"] == "full_sell":
                            self._close(trade, price, "MOONBAG_TRAIL")
                            continue

                    # Standard TP/SL
                    age_s = (datetime.now(timezone.utc)
                             .timestamp() - datetime.fromisoformat(
                                 trade.entry_time.replace("Z","")).timestamp())

                    if price >= trade.tp_price:
                        self._close(trade, price, "TP")
                    elif price <= trade.sl_price:
                        self._close(trade, price, "SL")
                    elif age_s >= int(os.getenv("TRADE_TIMEOUT_S", "300")):
                        self._close(trade, price, "TIMEOUT")

                except Exception as e:
                    log.warning("Monitor error %s: %s", mint[:8], e)

    def _partial_close(self, trade: PaperTrade, price: float, reason: str) -> None:
        """
        60% sold at 2x — reclaim that slice's value into capital and reduce
        the trade's remaining exposure so the final close only PnLs the rest.
        """
        # Cost basis being closed (60% of original entry) and its current value.
        partial_cost   = trade.size_sol * 0.60
        partial_value  = partial_cost * (price / trade.entry_price)
        partial_pnl    = partial_value - partial_cost

        self.capital            += partial_value
        self.daily_pnl          += partial_pnl
        self.peak                = max(self.peak, self.capital)

        trade.phase2_held        = True
        trade.partial_pnl_sol   += partial_pnl
        trade.remaining_size_sol = max(0.0, trade.remaining_size_sol - partial_cost)

        log.info(
            "PAPER PARTIAL %s reclaim=%.4f pnl=%+.4f remaining=%.4f (%s)",
            trade.symbol, partial_value, partial_pnl,
            trade.remaining_size_sol, reason,
        )
        self._tg(
            f"🌙 *MOONBAG PHASE 2* `{trade.symbol}`\n"
            f"Sold 60% @ 2x — `+{partial_value:.4f} SOL` reclaimed (`{partial_pnl:+.4f}` pnl)\n"
            f"40% riding free with trailing stop"
        )

    def _close(self, trade: PaperTrade, price: float, reason: str) -> None:
        # Compute PnL only on the portion of the entry still exposed.
        remaining       = trade.remaining_size_sol if trade.remaining_size_sol > 0 else trade.size_sol
        pnl_pct         = (price - trade.entry_price) / trade.entry_price
        remaining_value = remaining * (price / trade.entry_price)
        remaining_pnl   = remaining_value - remaining

        # Total PnL = realised partials + final remainder PnL.
        total_pnl       = trade.partial_pnl_sol + remaining_pnl

        trade.exit_time   = _now()
        trade.exit_price  = price
        trade.pnl_sol     = round(total_pnl, 6)
        trade.pnl_pct     = round(pnl_pct * 100, 2)
        trade.exit_reason = reason
        if trade.phase2_held:
            trade.phase2_pnl = round(remaining_pnl, 6)

        # Return the remaining capital (cost basis + its PnL) to the wallet.
        self.capital   += remaining_value
        self.peak       = max(self.peak, self.capital)
        self.daily_pnl += remaining_pnl

        # Use total_pnl for downstream stats / Telegram so partials are reflected.
        pnl_sol = total_pnl

        del self.open[trade.mint]
        self.closed.append(trade)
        self._save()

        # Update circadian + evolution + compounding
        if FEATURES_LOADED:
            circadian.record(pnl_pct)
            evolution.record(trade.mint, "entered", trade.score, reason, pnl_sol)
            compounder.record_pnl(pnl_sol)

        icon = "🟢" if pnl_sol > 0 else "🔴"
        log.info("PAPER CLOSE %s pnl=%+.4f reason=%s cap=%.4f",
                 trade.symbol, pnl_sol, reason, self.capital)

        self._tg(
            f"{icon} *PAPER CLOSE* `{trade.symbol}`\n"
            f"PnL:    `{pnl_sol:+.4f} SOL` (`{pnl_pct*100:+.1f}%`)\n"
            f"Reason: `{reason}`\n"
            f"Capital:`{self.capital:.4f} SOL`\n"
            f"DD:     `{self._drawdown()*100:.1f}%`"
        )

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _feature_breakdown(
        self, mint: str, name: str, symbol: str, transfers: list
    ) -> str:
        """
        Per-feature decision breakdown for transparency in alerts.
        Recomputes each individual feature so the user can see what fired.
        """
        if not FEATURES_LOADED:
            return ""
        lines: list[str] = ["*Features:*"]
        try:
            ne_score, ne_flags = name_entropy.score(name, symbol)
            lines.append(f"• Name entropy: `{ne_score}/100` {ne_flags or ''}")
        except Exception as e:
            lines.append(f"• Name entropy: err {e}")
        try:
            wr, wash = wash_detect.detect(transfers)
            lines.append(f"• Wash ratio: `{wr:.2f}` {'WASH' if wash else 'OK'}")
        except Exception as e:
            lines.append(f"• Wash: err {e}")
        try:
            grav = liq_gravity.score(mint)
            lines.append(f"• Liq gravity: `{grav:.0f}`")
        except Exception:
            lines.append("• Liq gravity: n/a")
        try:
            clear, fee = gas_oracle.is_clear()
            lines.append(f"• Gas: `{fee}µL` {'CLEAR' if clear else 'CONGESTED'}")
        except Exception:
            lines.append("• Gas: n/a")
        try:
            lines.append(f"• Circadian x: `{circadian.multiplier():.2f}`")
        except Exception:
            lines.append("• Circadian: n/a")
        try:
            lines.append(f"• Narrative x: `{narrative.score(name, symbol):.2f}`")
        except Exception:
            lines.append("• Narrative: n/a")
        try:
            coh, dirn = mtf.coherent(mint)
            lines.append(f"• MTF coherent: `{coh}` ({dirn})")
        except Exception:
            lines.append("• MTF: n/a")
        return "\n".join(lines)

    def _get_price(self, mint: str) -> Optional[float]:
        try:
            SOL = "So11111111111111111111111111111111111111112"
            r   = requests.get(
                f"https://price.jup.ag/v4/price?ids={mint}&vsToken={SOL}",
                timeout=3
            ).json()
            return r["data"][mint]["price"]
        except Exception:
            return None

    def _drawdown(self) -> float:
        return (self.peak - self.capital) / self.peak if self.peak > 0 else 0.0

    def _load_existing(self) -> list:
        """Load any prior closed trades so history is preserved across restarts."""
        try:
            with open(PAPER_LOG) as f:
                raw = json.load(f)
        except Exception:
            return []
        loaded: list = []
        for r in raw:
            try:
                loaded.append(PaperTrade(**{
                    k: v for k, v in r.items()
                    if k in PaperTrade.__dataclass_fields__
                }))
            except Exception:
                continue
        return loaded

    def _save(self) -> None:
        """
        Persist the full trade history to engine/paper_trades.json.
        Re-reads any rows already on disk that we don't have in memory and
        merges them in, so concurrent writers / restarts can't truncate
        history.
        """
        try:
            os.makedirs(os.path.dirname(PAPER_LOG) or ".", exist_ok=True)
            existing: list = []
            try:
                with open(PAPER_LOG) as f:
                    existing = json.load(f) or []
            except Exception:
                existing = []

            seen_ids = {t.id for t in self.closed if getattr(t, "id", None)}
            preserved = [
                r for r in existing
                if isinstance(r, dict) and r.get("id") not in seen_ids
            ]

            merged = preserved + [asdict(t) for t in self.closed]
            tmp = PAPER_LOG + ".tmp"
            with open(tmp, "w") as f:
                json.dump(merged, f, indent=2, default=str)
            os.replace(tmp, PAPER_LOG)
        except Exception as e:
            log.warning("Save failed: %s", e)

    def _tg(self, msg: str) -> None:
        now = time.time()
        if now - self._tg_last < 0.35:
            time.sleep(0.35)
        self._tg_last = time.time()
        try:
            requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
                timeout=5,
            )
        except Exception:
            pass

    def stats(self) -> dict:
        trades = self.closed
        if not trades:
            return {"trades": 0, "capital": self.capital}
        wins     = [t for t in trades if t.pnl_sol > 0]
        losses   = [t for t in trades if t.pnl_sol <= 0]
        total_pnl = sum(t.pnl_sol for t in trades)
        win_rate  = len(wins) / len(trades) * 100

        # Corrected win rate with survivor bias
        corrected, true_wr = survivor.correct(
            [{"pnl_sol": t.pnl_sol, "entry_sol": t.size_sol} for t in trades],
            self.detected
        ) if FEATURES_LOADED else (trades, win_rate/100)

        # Expectancy
        avg_win  = sum(t.pnl_sol for t in wins)  / len(wins)  if wins  else 0
        avg_loss = sum(t.pnl_sol for t in losses) / len(losses) if losses else 0
        expectancy = (true_wr * avg_win) + ((1-true_wr) * avg_loss)

        return {
            "trades":          len(trades),
            "detected":        self.detected,
            "open":            len(self.open),
            "capital":         round(self.capital, 4),
            "total_pnl":       round(total_pnl, 4),
            "roi_pct":         round(total_pnl / CAPITAL * 100, 2),
            "win_rate_raw":    round(win_rate, 1),
            "win_rate_true":   round(true_wr * 100, 1),
            "avg_win_sol":     round(avg_win, 4),
            "avg_loss_sol":    round(avg_loss, 4),
            "expectancy":      round(expectancy, 4),
            "max_drawdown":    round(self._drawdown() * 100, 1),
            "daily_pnl":       round(self.daily_pnl, 4),
        }


# ══════════════════════════════════════════════════════════════════════════════
# HISTORICAL BACKTESTER
# ══════════════════════════════════════════════════════════════════════════════

class HistoricalBacktester:
    """
    Replays paper_trades.json to validate feature effectiveness.
    Also accepts a CSV path for pre-existing token launch data.

    Computes:
      - Feature contribution analysis (which features add alpha)
      - Parameter sweep (TP/SL/score threshold combinations)
      - Sharpe ratio, max drawdown, expectancy
      - Self-Evolution threshold suggestions
    """

    # ── CSV ingestion ─────────────────────────────────────────────────────────
    #
    # Expected CSV schema (header row required, column order flexible):
    #   mint            — token mint address              (required)
    #   entry_price     — fill price in SOL               (required)
    #   exit_price      — close price in SOL              (required)
    #   score           — 0–100 pre-trade score           (optional, default 0)
    #   liquidity       — pool liquidity in SOL           (optional, default 0)
    #   timestamp       — entry time (ISO-8601 or unix s) (optional)
    #   exit_timestamp  — exit time  (ISO-8601 or unix s) (optional)
    #   size_sol        — SOL committed at entry          (optional, default 5% of CAPITAL)
    #   rug_score       — 0–100 rug detector score        (optional, default 0)
    #   kelly_pct       — Kelly fraction used             (optional, default 0)
    #   symbol          — token symbol                    (optional, default mint[:6])
    #   phase2_held     — 1/true if moonbag rode          (optional, default false)
    #   phase2_pnl      — moonbag remainder PnL in SOL    (optional, default 0)
    #
    # Any extra columns in the CSV are ignored. Rows missing required fields
    # are skipped with a warning.
    REQUIRED_CSV_COLS = ("mint", "entry_price", "exit_price")

    @staticmethod
    def _parse_csv_ts(value: str) -> str:
        """Return an ISO-8601 timestamp string from either unix seconds or ISO input."""
        if not value:
            return ""
        v = value.strip()
        if not v:
            return ""
        try:
            if v.replace(".", "", 1).isdigit():
                return datetime.fromtimestamp(float(v), tz=timezone.utc).isoformat()
            return datetime.fromisoformat(v.rstrip("Z")).replace(
                tzinfo=timezone.utc
            ).isoformat()
        except Exception:
            return v  # pass through; downstream code only needs truthiness

    @classmethod
    def load_csv_trades(cls, csv_path: str | Path) -> list[dict]:
        """
        Parse a CSV of past PumpFun / Raydium token launches into the same
        dict shape `_analyse` expects from `paper_trades.json`.
        """
        path = Path(csv_path)
        if not path.exists():
            raise FileNotFoundError("CSV file not found")

        trades: list[dict] = []
        skipped = 0
        with open(path, newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for i, row in enumerate(reader, start=2):  # start=2 → header is line 1
                missing = [c for c in cls.REQUIRED_CSV_COLS if not row.get(c)]
                if missing:
                    log.warning("CSV row %d missing %s — skipped", i, missing)
                    skipped += 1
                    continue
                try:
                    mint        = row["mint"].strip()
                    entry_price = float(row["entry_price"])
                    exit_price  = float(row["exit_price"])
                    if entry_price <= 0:
                        raise ValueError("entry_price must be > 0")

                    size_sol = float(row.get("size_sol") or CAPITAL * 0.05)
                    pnl_pct  = (exit_price - entry_price) / entry_price
                    pnl_sol  = size_sol * pnl_pct

                    phase2_raw = str(row.get("phase2_held", "")).strip().lower()
                    phase2_held = phase2_raw in ("1", "true", "yes", "y", "t")

                    trade = {
                        "id":           f"csv_{i}_{mint[:6]}",
                        "mint":         mint,
                        "symbol":       (row.get("symbol") or mint[:6]).strip(),
                        "entry_time":   cls._parse_csv_ts(row.get("timestamp", "")),
                        "exit_time":    cls._parse_csv_ts(row.get("exit_timestamp", ""))
                                          or cls._parse_csv_ts(row.get("timestamp", ""))
                                          or _now(),
                        "entry_price":  entry_price,
                        "exit_price":   exit_price,
                        "size_sol":     size_sol,
                        "pnl_sol":      round(pnl_sol, 6),
                        "pnl_pct":      round(pnl_pct * 100, 2),
                        "exit_reason":  row.get("exit_reason", "CSV"),
                        "score":        int(float(row.get("score") or 0)),
                        "rug_score":    int(float(row.get("rug_score") or 0)),
                        "kelly_pct":    float(row.get("kelly_pct") or 0.0),
                        "liquidity":    float(row.get("liquidity") or 0.0),
                        "phase2_held":  phase2_held,
                        "phase2_pnl":   float(row.get("phase2_pnl") or 0.0),
                        "flags":        [],
                    }
                    trades.append(trade)
                except (ValueError, KeyError) as exc:
                    log.warning("CSV row %d malformed (%s) — skipped", i, exc)
                    skipped += 1

        log.info("CSV ingest: %d trades from %s (%d skipped)", len(trades), path.name, skipped)
        return trades

    def run_from_csv(self, csv_path: str | Path, detected_count: int = 0) -> dict:
        """
        Run the full analysis pipeline (parameter sweep, Sharpe, verdict,
        feature effectiveness) against a CSV of past token launches.
        """
        try:
            trades = self.load_csv_trades(csv_path)
        except FileNotFoundError as exc:
            return {"error": str(exc)}
        except Exception as exc:
            return {"error": f"Failed to parse CSV: {exc}"}

        if len(trades) < 10:
            return {
                "error": f"Only {len(trades)} usable rows in CSV. Need 10+ for analysis.",
                "rows_loaded": len(trades),
            }

        # Detected count defaults to row count when caller hasn't supplied one,
        # so the survivor-bias corrector has a sane denominator.
        if detected_count <= 0:
            detected_count = len(trades)

        report = self._analyse(trades, detected_count=detected_count)
        report["source"] = f"csv:{Path(csv_path).name}"
        report["rows_loaded"] = len(trades)
        return report

    def run_from_paper_log(self, detected_count: int = 0) -> dict:
        """
        Analyse existing paper trades to find optimal parameters.
        `detected_count` is the total number of tokens the live engine
        evaluated this session — used by SurvivorBiasCorrector to inject
        phantom losses for invisible exits.
        """
        try:
            with open(PAPER_LOG) as f:
                trades = json.load(f)
        except Exception:
            return {"error": "No paper trade log found. Run paper mode first."}

        if len(trades) < 10:
            return {"error": f"Only {len(trades)} trades. Need 10+ for analysis."}

        return self._analyse(trades, detected_count=detected_count)

    def parameter_sweep(self, trades: list) -> list:
        """
        Tests every combination of TP/SL/score threshold.
        Finds the combination that maximises expectancy.
        Efficient: O(n * combos) where combos is small.
        """
        tp_range    = [0.15, 0.20, 0.25, 0.30]
        sl_range    = [0.05, 0.07, 0.10]
        score_range = [45, 55, 65, 75]

        results = []

        for tp in tp_range:
            for sl in sl_range:
                for min_score in score_range:
                    filtered = [
                        t for t in trades
                        if t.get("score", 0) >= min_score
                    ]
                    if len(filtered) < 5:
                        continue

                    # Simulate with these parameters
                    cap   = CAPITAL
                    pnls  = []
                    for t in filtered:
                        entry = t.get("entry_price", 1)
                        exit_ = t.get("exit_price", entry)
                        pct   = (exit_ - entry) / entry if entry else 0
                        # Clip to simulated TP/SL
                        pct   = min(pct, tp)
                        pct   = max(pct, -sl)
                        size  = cap * 0.05
                        pnl   = size * pct
                        pnls.append(pnl)
                        cap  += pnl

                    if not pnls:
                        continue

                    wins  = [p for p in pnls if p > 0]
                    losses= [p for p in pnls if p <= 0]
                    wr    = len(wins) / len(pnls)
                    aw    = sum(wins)  / len(wins)   if wins   else 0
                    al    = sum(losses)/ len(losses) if losses else 0
                    exp   = (wr * aw) + ((1-wr) * al)
                    sharpe= self._sharpe(pnls)

                    results.append({
                        "tp":           tp,
                        "sl":           sl,
                        "min_score":    min_score,
                        "trades":       len(filtered),
                        "win_rate":     round(wr * 100, 1),
                        "expectancy":   round(exp, 5),
                        "sharpe":       round(sharpe, 3),
                        "final_cap":    round(cap, 4),
                    })

        # Sort by expectancy
        results.sort(key=lambda x: x["expectancy"], reverse=True)
        return results[:10]   # top 10 parameter sets

    def _analyse(self, trades: list, detected_count: int = 0) -> dict:
        closed = [t for t in trades if t.get("exit_time")]
        pnls   = [t.get("pnl_sol", 0) for t in closed]
        wins   = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]

        if not pnls:
            return {"error": "No closed trades found."}

        wr         = len(wins) / len(pnls)
        avg_win    = sum(wins)  / len(wins)   if wins   else 0
        avg_loss   = sum(losses)/ len(losses) if losses else 0
        expectancy = (wr * avg_win) + ((1-wr) * avg_loss)
        total_pnl  = sum(pnls)
        sharpe     = self._sharpe(pnls)
        max_dd     = self._max_drawdown(pnls, CAPITAL)

        # ─── Survivor-bias-corrected metrics ────────────────────────────
        # Phantom losses are injected for every detected token that never
        # produced a trade — these are the invisible exits we'd otherwise
        # be blind to. Without a detected_count we can't correct.
        wr_true_pct        = round(wr * 100, 1)
        expectancy_true    = round(expectancy, 5)
        phantom_count      = 0
        if FEATURES_LOADED and detected_count > len(closed):
            corrected, wr_true = survivor.correct(closed, detected_count)
            wr_true_pct        = round(wr_true * 100, 1)
            phantom_count      = len(corrected) - len(closed)
            cor_pnls           = [t.get("pnl_sol", 0) for t in corrected]
            cor_wins           = [p for p in cor_pnls if p > 0]
            cor_losses         = [p for p in cor_pnls if p <= 0]
            cor_aw             = sum(cor_wins)/len(cor_wins)     if cor_wins   else 0
            cor_al             = sum(cor_losses)/len(cor_losses) if cor_losses else 0
            expectancy_true    = round((wr_true * cor_aw) + ((1-wr_true) * cor_al), 5)

        # Feature effectiveness
        feature_stats = self._feature_effectiveness(trades)

        # Parameter sweep
        best_params = self.parameter_sweep(trades)

        # Self-evolution suggestions
        suggestions = evolution.auto_tune() if FEATURES_LOADED else {}

        ready = (
            expectancy_true > 0
            and wr_true_pct  > 40.0
            and max_dd       < 30
        )

        report = {
            "═══ BACKTEST REPORT ═══": "",
            "total_trades":         len(pnls),
            "detected_count":       detected_count,
            "phantom_losses":       phantom_count,
            "win_rate_pct":         round(wr * 100, 1),
            "win_rate_true_pct":    wr_true_pct,
            "avg_win_sol":          round(avg_win, 5),
            "avg_loss_sol":         round(avg_loss, 5),
            "expectancy_sol":       round(expectancy, 5),
            "expectancy_true_sol":  expectancy_true,
            "total_pnl_sol":        round(total_pnl, 5),
            "roi_pct":              round(total_pnl / CAPITAL * 100, 2),
            "sharpe_ratio":         sharpe,
            "max_drawdown_pct":     round(max_dd, 1),
            "═══ TOP PARAMETER SETS ═══": "",
            "best_params":          best_params[:3],
            "═══ FEATURE EFFECTIVENESS ═══": "",
            "feature_stats":        feature_stats,
            "═══ EVOLUTION SUGGESTIONS ═══": "",
            "suggestions":          suggestions,
            "═══ VERDICT ═══": "",
            "ready_for_live":       ready,
            "verdict":              self._verdict(expectancy_true, wr_true_pct/100, max_dd, sharpe),
        }

        # Print to console
        for k, v in report.items():
            if "═══" in k:
                print(f"\n{k}")
            else:
                print(f"  {k:<25} {v}")

        return report

    def _feature_effectiveness(self, trades: list) -> dict:
        """Which features correlate with winning trades?"""
        stats = {}

        # Rug score correlation
        high_rug  = [t for t in trades if t.get("rug_score", 0) >= 75]
        low_rug   = [t for t in trades if t.get("rug_score", 0) < 75]
        if high_rug and low_rug:
            wr_high = sum(1 for t in high_rug if t.get("pnl_sol",0) > 0) / len(high_rug)
            wr_low  = sum(1 for t in low_rug  if t.get("pnl_sol",0) > 0) / len(low_rug)
            stats["rug_filter_alpha"] = round(wr_high - wr_low, 3)

        # Kelly sizing vs fixed sizing
        kelly_trades = [t for t in trades if t.get("kelly_pct", 0) > 0]
        if kelly_trades:
            avg_kelly_pnl = sum(t.get("pnl_sol",0) for t in kelly_trades) / len(kelly_trades)
            stats["avg_pnl_kelly_sized"] = round(avg_kelly_pnl, 5)

        # Moonbag effectiveness
        moonbag_trades = [t for t in trades if t.get("phase2_held")]
        if moonbag_trades:
            stats["moonbag_trades"] = len(moonbag_trades)
            stats["moonbag_extra_pnl"] = round(
                sum(t.get("phase2_pnl",0) for t in moonbag_trades), 4
            )

        return stats

    @staticmethod
    def _sharpe(pnls: list) -> float:
        if len(pnls) < 2:
            return 0.0
        mean = sum(pnls) / len(pnls)
        std  = math.sqrt(sum((p-mean)**2 for p in pnls) / len(pnls))
        return round((mean / std) * math.sqrt(252) if std > 0 else 0.0, 3)

    @staticmethod
    def _max_drawdown(pnls: list, initial: float) -> float:
        cap  = initial
        peak = initial
        max_dd = 0.0
        for p in pnls:
            cap  += p
            peak  = max(peak, cap)
            dd    = (peak - cap) / peak * 100
            max_dd = max(max_dd, dd)
        return max_dd

    @staticmethod
    def _verdict(expectancy: float, wr: float,
                 max_dd: float, sharpe: float) -> str:
        if expectancy <= 0:
            return "❌ NEGATIVE EDGE — do not go live. Tune parameters."
        if wr < 0.35:
            return "⚠️  LOW WIN RATE — increase min_score threshold."
        if max_dd > 30:
            return "⚠️  HIGH DRAWDOWN — tighten SL or reduce position size."
        if sharpe < 0.5:
            return "⚠️  LOW SHARPE — inconsistent returns. Paper trade longer."
        if expectancy > 0 and wr >= 0.45 and max_dd < 20 and sharpe > 1.0:
            return "✅ STRONG EDGE — ready to go live with DRY_RUN=false."
        return "🟡 MARGINAL EDGE — paper trade 10 more trades before live."


# ══════════════════════════════════════════════════════════════════════════════
# FASTAPI ROUTES
# ══════════════════════════════════════════════════════════════════════════════

engine     = PaperEngine()
backtester = HistoricalBacktester()


@app.on_event("startup")
async def startup():
    asyncio.create_task(engine.monitor_loop())
    log.info("Paper engine started. Capital=%.2f SOL", CAPITAL)
    _tg_send(
        f"📝 *PAPER MODE ACTIVE*\n"
        f"Capital: `{CAPITAL} SOL`\n"
        f"Features: `{'17 loaded' if FEATURES_LOADED else 'basic mode'}`\n"
        f"Target: 10 trades → then review `/backtest`"
    )


@app.post("/webhook")
async def webhook(req: Request):
    """Receives Helius Enhanced Transaction events."""
    try:
        events = await req.json()
        if not isinstance(events, list):
            events = [events]

        for event in events:
            mint  = None
            liq   = 0.0
            xfers = event.get("tokenTransfers", [])

            for t in xfers:
                if t.get("fromUserAccount") == "11111111111111111111111111111111":
                    mint = t.get("mint")
                    break

            for t in event.get("nativeTransfers", []):
                liq += t.get("amount", 0) / 1e9

            if mint:
                name    = event.get("description", "")[:20] or "Unknown"
                symbol  = mint[:6].upper()
                creator = event.get("feePayer", "")
                asyncio.create_task(
                    engine.on_new_token(mint, name, symbol, creator, liq, xfers)
                )

        return JSONResponse({"ok": True})
    except Exception as e:
        log.error("Webhook error: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/", dependencies=[Depends(require_api_key)])
async def status():
    return engine.stats()


@app.get("/stats", dependencies=[Depends(require_api_key)])
async def stats():
    """Live engine state — same payload as `/`. Required by the spec."""
    return engine.stats()


@app.get("/backtest", dependencies=[Depends(require_api_key), Depends(_heavy_rate_limit)])
async def backtest():
    """Run historical analysis on paper trade log."""
    return backtester.run_from_paper_log(detected_count=engine.detected)


MAX_CSV_BYTES = int(os.environ.get("MAX_CSV_BYTES", str(5 * 1024 * 1024)))  # 5 MB
CSV_DOWNLOAD_TIMEOUT = float(os.environ.get("CSV_DOWNLOAD_TIMEOUT", "15"))


def _download_csv_to_tempfile(url: str) -> str:
    """
    Stream a remote CSV into a temp file with a hard size cap.
    Returns the temp file path on success. Raises ValueError on any failure
    with a user-friendly message.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or 'none'}")

    try:
        resp = requests.get(url, stream=True, timeout=CSV_DOWNLOAD_TIMEOUT,
                            allow_redirects=True)
    except requests.RequestException as exc:
        raise ValueError(f"download failed: {exc}") from exc

    if resp.status_code != 200:
        resp.close()
        raise ValueError(f"download failed: HTTP {resp.status_code}")

    declared = resp.headers.get("Content-Length")
    if declared and declared.isdigit() and int(declared) > MAX_CSV_BYTES:
        resp.close()
        raise ValueError(
            f"CSV too large: {int(declared)} bytes > {MAX_CSV_BYTES}"
        )

    fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="backtest_dl_")
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > MAX_CSV_BYTES:
                    raise ValueError(
                        f"CSV too large: exceeded {MAX_CSV_BYTES} bytes"
                    )
                out.write(chunk)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    finally:
        resp.close()

    return tmp_path


@app.get("/backtest_csv", dependencies=[Depends(require_api_key)])
async def backtest_csv_get():
    """
    Server-side path access has been removed for security.
    Use POST /backtest_csv to upload a CSV file directly.
    """
    return JSONResponse(
        {"error": "Server-side path access is disabled. "
                  "Use POST /backtest_csv to upload a CSV file directly."},
        status_code=410,
    )


@app.post("/backtest_csv", dependencies=[Depends(require_api_key), Depends(_heavy_rate_limit)])
async def backtest_csv_post(
    file: UploadFile = File(..., description="CSV file of past launches"),
    detected: int = Form(0),
):
    """
    Upload a CSV of past launches and run the historical backtester.

    Form fields:
      file     — multipart CSV upload (max 5 MB, override via MAX_CSV_BYTES)
      detected — total tokens scanned (defaults to row count)
    """
    fd, tmp_path = tempfile.mkstemp(suffix=".csv", prefix="backtest_up_")
    written = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while True:
                chunk = await file.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_CSV_BYTES:
                    return JSONResponse(
                        {"error": f"upload too large: max {MAX_CSV_BYTES} bytes"},
                        status_code=413,
                    )
                out.write(chunk)

        if written == 0:
            return JSONResponse({"error": "empty upload"}, status_code=400)

        report = backtester.run_from_csv(tmp_path, detected_count=detected)
        if "error" in report:
            return JSONResponse(report, status_code=400)
        report["uploaded_filename"] = file.filename
        report["uploaded_bytes"]    = written
        return report
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@app.get("/params", dependencies=[Depends(require_api_key), Depends(_heavy_rate_limit)])
async def best_params():
    """Show top parameter combinations from sweep."""
    try:
        with open(PAPER_LOG) as f:
            trades = json.load(f)
        return backtester.parameter_sweep(trades)
    except Exception as e:
        return {"error": str(e)}


@app.get("/evolve", dependencies=[Depends(require_api_key), Depends(_heavy_rate_limit)])
async def evolve():
    """Run self-evolution analysis and get threshold suggestions."""
    return evolution.auto_tune() if FEATURES_LOADED else {"error": "features not loaded"}


@app.get("/health")
async def health():
    return {"ok": True, "paper_trades": len(engine.closed), "open": len(engine.open)}


# ── Telegram polling ──────────────────────────────────────────────────────────
_offset = 0

async def tg_commands():
    global _offset
    while True:
        try:
            r = requests.get(
                f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                params={"offset": _offset, "timeout": 10, "limit": 5},
                timeout=15,
            ).json()
            for upd in r.get("result", []):
                _offset = upd["update_id"] + 1
                msg  = upd.get("message", {})
                chat = str(msg.get("chat", {}).get("id", ""))
                text = msg.get("text", "").strip().lower()
                if chat != str(TG_CHAT):
                    continue
                if text == "/stats":
                    _tg_send(json.dumps(engine.stats(), indent=2))
                elif text == "/backtest":
                    r2 = backtester.run_from_paper_log(detected_count=engine.detected)
                    _tg_send(_format_backtest_msg("📊 *Backtest Results*", r2))
                elif text.startswith("/backtest_csv"):
                    raw = msg.get("text", "").strip()
                    parts = raw.split(maxsplit=1)
                    if len(parts) != 2 or not parts[1].strip():
                        _tg_send(
                            "📥 *Backtest CSV*\n"
                            "Usage: `/backtest_csv <url>`\n"
                            "URL must point to a CSV (max "
                            f"`{MAX_CSV_BYTES // 1024} KB`)."
                        )
                    else:
                        url = parts[1].strip()
                        _tg_send(f"📥 Downloading CSV from `{url[:60]}` …")
                        try:
                            tmp = await asyncio.to_thread(
                                _download_csv_to_tempfile, url
                            )
                        except ValueError as exc:
                            _tg_send(f"❌ *CSV download failed*\n`{exc}`")
                        else:
                            try:
                                r2 = backtester.run_from_csv(tmp)
                            finally:
                                try: os.unlink(tmp)
                                except OSError: pass
                            _tg_send(_format_backtest_msg(
                                f"📊 *CSV Backtest* `{url[-40:]}`", r2
                            ))
                elif text == "/evolve":
                    s = evolution.auto_tune() if FEATURES_LOADED else {}
                    _tg_send(f"🧬 *Evolution*\n`{json.dumps(s, indent=2)}`")
        except Exception as e:
            log.warning("TG poll: %s", e)
        await asyncio.sleep(2)


@app.on_event("startup")
async def start_tg():
    asyncio.create_task(tg_commands())


def _format_backtest_msg(title: str, r2: dict) -> str:
    """Render a backtester report dict into a Telegram-Markdown message."""
    if "error" in r2:
        return f"{title}\n`{r2['error']}`"
    params = r2.get("best_params", []) or []
    params_lines = "\n".join(
        f"  {i+1}. tp=`{p.get('tp')}` sl=`{p.get('sl')}` "
        f"min_score=`{p.get('min_score')}` "
        f"wr=`{p.get('win_rate')}%` exp=`{p.get('expectancy')}`"
        for i, p in enumerate(params[:3])
    ) or "  (none)"
    feats = r2.get("feature_stats", {}) or {}
    feat_lines = "\n".join(
        f"  • `{k}`: `{v}`" for k, v in list(feats.items())[:5]
    ) or "  (none)"
    return (
        f"{title}\n"
        f"Trades: `{r2.get('total_trades', 0)}` "
        f"(detected: `{r2.get('detected_count', 0)}`, "
        f"phantoms: `{r2.get('phantom_losses', 0)}`)\n"
        f"Win Rate: `{r2.get('win_rate_pct', 0)}%` "
        f"(true: `{r2.get('win_rate_true_pct', 0)}%`)\n"
        f"Expectancy: `{r2.get('expectancy_sol', 0)} SOL` "
        f"(true: `{r2.get('expectancy_true_sol', 0)} SOL`)\n"
        f"Total PnL: `{r2.get('total_pnl_sol', 0)} SOL` "
        f"(ROI `{r2.get('roi_pct', 0)}%`)\n"
        f"Sharpe: `{r2.get('sharpe_ratio', 0)}` | "
        f"Max DD: `{r2.get('max_drawdown_pct', 0)}%`\n"
        f"\n*Top 3 Parameter Sets:*\n{params_lines}\n"
        f"\n*Feature Effectiveness:*\n{feat_lines}\n"
        f"\n*Verdict:* {r2.get('verdict', '—')}\n"
        f"Ready for LIVE: `{r2.get('ready_for_live', False)}`"
    )


def _tg_send(msg: str) -> None:
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
