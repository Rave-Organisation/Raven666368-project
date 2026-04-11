"""
Alpha Engine — Backtesting Harness
===================================
Event-driven historical replay engine for validating the signal classifier
and risk engine against past market data.

Three operating modes
---------------------
  historical      Full offline replay against SQLite / CSV data.
  paper_live      Async real-time loop consuming a live WebSocket feed.
  shadow          Runs in parallel with the live bot, logging divergence.

Signal classifier contract
--------------------------
  fn(event: MarketEvent) -> Optional[Signal]
  Replace `null_classifier` with your real classifier import.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, auto
from typing import Any, Callable, Optional

from engine.backtesting.data_loader import DataLoader, MarketEvent
from engine.backtesting.metrics import MetricsEngine, PerformanceReport
from engine.infrastructure.logger import AuditLogger, get_logger

log = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Domain types
# ──────────────────────────────────────────────────────────────────────────────

class Side(Enum):
    LONG  = auto()
    SHORT = auto()


class TradeResult(Enum):
    WIN       = auto()
    LOSS      = auto()
    BREAKEVEN = auto()


@dataclass
class Signal:
    timestamp:        datetime
    token_mint:       str
    conviction_score: float           # 0–100
    direction:        Side
    source_tags:      list[str]       # e.g. ["smc_bos", "sentiment_spike"]
    raw_features:     dict[str, Any]


@dataclass
class SimulatedTrade:
    id:                str
    signal:            Signal
    entry_price:       float
    entry_timestamp:   datetime
    size_sol:          float
    stop_loss_price:   float
    take_profit_price: float

    exit_price:     Optional[float]       = None
    exit_timestamp: Optional[datetime]    = None
    pnl_sol:        Optional[float]       = None
    result:         Optional[TradeResult] = None
    exit_reason:    Optional[str]         = None  # tp_hit | sl_hit | timeout | end_of_data

    @property
    def is_open(self) -> bool:
        return self.exit_price is None

    def close(self, price: float, timestamp: datetime, reason: str) -> None:
        self.exit_price     = price
        self.exit_timestamp = timestamp
        self.exit_reason    = reason

        direction_sign = 1 if self.signal.direction == Side.LONG else -1
        pct_move       = (price - self.entry_price) / self.entry_price
        self.pnl_sol   = pct_move * direction_sign * self.size_sol

        if self.pnl_sol > 0.0001:
            self.result = TradeResult.WIN
        elif self.pnl_sol < -0.0001:
            self.result = TradeResult.LOSS
        else:
            self.result = TradeResult.BREAKEVEN


@dataclass
class PortfolioState:
    initial_capital_sol:   float
    capital_sol:           float
    peak_capital_sol:      float
    open_trades:           list[SimulatedTrade] = field(default_factory=list)
    closed_trades:         list[SimulatedTrade] = field(default_factory=list)
    daily_loss_sol:        float = 0.0
    circuit_breaker_active: bool = False

    @property
    def total_trades(self) -> int:
        return len(self.closed_trades)

    @property
    def drawdown_pct(self) -> float:
        if self.peak_capital_sol == 0:
            return 0.0
        return (self.peak_capital_sol - self.capital_sol) / self.peak_capital_sol * 100

    def update_peak(self) -> None:
        if self.capital_sol > self.peak_capital_sol:
            self.peak_capital_sol = self.capital_sol


# ──────────────────────────────────────────────────────────────────────────────
# Risk engine  (mirrors live bot risk parameters exactly)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class RiskConfig:
    initial_capital_sol:          float = 10.0
    base_risk_pct:                float = 0.01    # 1% base risk per trade
    conviction_punt_pct:          float = 0.05    # 5% for high-conviction trades
    conviction_punt_threshold:    float = 90.0
    stop_loss_pct:                float = 0.05
    take_profit_pct:              float = 0.15
    max_open_positions:           int   = 3
    circuit_breaker_drawdown_pct: float = 30.0
    daily_loss_limit_pct:         float = 0.10
    trade_timeout_seconds:        int   = 300


class RiskEngine:
    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def position_size(self, portfolio: PortfolioState, signal: Signal) -> float:
        cfg = self.config

        if portfolio.circuit_breaker_active:
            log.warning("Trade rejected — circuit breaker active.")
            return 0.0

        if len(portfolio.open_trades) >= cfg.max_open_positions:
            log.debug("Trade rejected — max open positions (%d).", cfg.max_open_positions)
            return 0.0

        if portfolio.drawdown_pct >= cfg.circuit_breaker_drawdown_pct:
            portfolio.circuit_breaker_active = True
            log.critical("CIRCUIT BREAKER TRIGGERED. Drawdown=%.1f%%.", portfolio.drawdown_pct)
            return 0.0

        if abs(portfolio.daily_loss_sol) / portfolio.initial_capital_sol >= cfg.daily_loss_limit_pct:
            log.warning("Daily loss limit reached (%.1f%%).", cfg.daily_loss_limit_pct * 100)
            return 0.0

        risk_pct = (
            cfg.conviction_punt_pct
            if signal.conviction_score >= cfg.conviction_punt_threshold
            else cfg.base_risk_pct
        )
        size = portfolio.capital_sol * risk_pct
        return min(size, portfolio.capital_sol * 0.95)

    def build_trade(self, signal: Signal, entry_price: float, size_sol: float) -> SimulatedTrade:
        cfg = self.config
        if signal.direction == Side.LONG:
            sl = entry_price * (1 - cfg.stop_loss_pct)
            tp = entry_price * (1 + cfg.take_profit_pct)
        else:
            sl = entry_price * (1 + cfg.stop_loss_pct)
            tp = entry_price * (1 - cfg.take_profit_pct)

        return SimulatedTrade(
            id                = str(uuid.uuid4())[:8],
            signal            = signal,
            entry_price       = entry_price,
            entry_timestamp   = signal.timestamp,
            size_sol          = size_sol,
            stop_loss_price   = sl,
            take_profit_price = tp,
        )


# ──────────────────────────────────────────────────────────────────────────────
# Signal classifier interface
# ──────────────────────────────────────────────────────────────────────────────

SignalClassifierFn = Callable[[MarketEvent], Optional[Signal]]


def null_classifier(event: MarketEvent) -> Optional[Signal]:
    """
    Stub classifier — replace with your real import.

    Example:
        from signal_classifier.classifier import AlphaClassifier
        clf = AlphaClassifier()
        harness = BacktestHarness(classifier=clf.classify, ...)
    """
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Core harness
# ──────────────────────────────────────────────────────────────────────────────

class BacktestHarness:
    """
    Replays a MarketEvent stream through the classifier and risk engine.
    """

    def __init__(
        self,
        classifier:   SignalClassifierFn = null_classifier,
        risk_config:  RiskConfig | None = None,
        slippage_bps: int   = 30,
        fee_sol:      float = 0.000_025,
        audit:        AuditLogger | None = None,
    ) -> None:
        self.classifier      = classifier
        self.risk            = RiskEngine(risk_config or RiskConfig())
        self.slippage_factor = 1 + slippage_bps / 10_000
        self.fee_sol         = fee_sol
        self.audit           = audit or AuditLogger()
        self._metrics        = MetricsEngine()
        self._event_count    = 0

        cfg = self.risk.config
        self.portfolio = PortfolioState(
            initial_capital_sol = cfg.initial_capital_sol,
            capital_sol         = cfg.initial_capital_sol,
            peak_capital_sol    = cfg.initial_capital_sol,
        )

    def run(self, data_loader: DataLoader) -> PerformanceReport:
        log.info("Backtest run started (historical mode).")
        for event in data_loader.stream():
            self._process_event(event)

        self._force_close_all("end_of_data")
        report = self._metrics.compute(self.portfolio)
        report.print_summary()
        self.audit.record_bot_event(
            "backtest_complete",
            {"total_events": self._event_count, "total_trades": self.portfolio.total_trades},
        )
        return report

    async def run_paper_live(
        self,
        data_loader:           DataLoader,
        tick_interval_seconds: float = 0.1,
    ) -> None:
        log.info("Paper-live mode started — no on-chain transactions will be sent.")
        self.audit.record_bot_event("paper_live_start")
        try:
            async for event in data_loader.stream_async():
                self._process_event(event)
                await asyncio.sleep(tick_interval_seconds)
        except asyncio.CancelledError:
            log.info("Paper-live cancelled. Closing open positions.")
            self._force_close_all("cancelled")
        finally:
            report = self._metrics.compute(self.portfolio)
            report.print_summary()
            self.audit.record_bot_event("paper_live_end")

    async def run_shadow(
        self,
        data_loader:      DataLoader,
        live_decision_fn: Callable[[MarketEvent], Optional[Signal]],
    ) -> None:
        log.info("Shadow mode started — comparing harness vs live bot decisions.")
        async for event in data_loader.stream_async():
            harness_signal = self.classifier(event)
            live_signal    = live_decision_fn(event)

            if bool(harness_signal) != bool(live_signal):
                log.warning(
                    "SHADOW DIVERGENCE on %s — harness=%s live=%s",
                    event.token_mint[:8],
                    "SIGNAL" if harness_signal else "NO_SIGNAL",
                    "SIGNAL" if live_signal    else "NO_SIGNAL",
                )
                self.audit.record_bot_event("shadow_divergence", {
                    "token_mint":    event.token_mint,
                    "harness_score": harness_signal.conviction_score if harness_signal else None,
                    "live_score":    live_signal.conviction_score    if live_signal    else None,
                    "event_ts":      event.timestamp.isoformat(),
                })

    def _process_event(self, event: MarketEvent) -> None:
        self._event_count += 1
        self._tick_open_positions(event)

        signal = self.classifier(event)
        if signal is None:
            return

        size_sol = self.risk.position_size(self.portfolio, signal)
        if size_sol <= 0:
            return

        fill_price = event.price * self.slippage_factor
        trade      = self.risk.build_trade(signal, fill_price, size_sol)

        self.portfolio.capital_sol -= size_sol + self.fee_sol
        self.portfolio.open_trades.append(trade)

        self.audit.record_trade_open(
            trade_id         = trade.id,
            token_mint       = signal.token_mint,
            entry_price      = fill_price,
            size_sol         = size_sol,
            stop_loss        = trade.stop_loss_price,
            take_profit      = trade.take_profit_price,
            conviction_score = signal.conviction_score,
        )
        log.debug(
            "OPEN  %s | id=%s | size=%.4f | score=%.0f | entry=%.6f",
            signal.token_mint[:8], trade.id, size_sol, signal.conviction_score, fill_price,
        )

    def _tick_open_positions(self, event: MarketEvent) -> None:
        still_open: list[SimulatedTrade] = []
        for trade in self.portfolio.open_trades:
            closed  = False
            price   = event.price
            is_long = trade.signal.direction == Side.LONG

            if is_long:
                if price <= trade.stop_loss_price:
                    trade.close(trade.stop_loss_price, event.timestamp, "sl_hit")
                    closed = True
                elif price >= trade.take_profit_price:
                    trade.close(trade.take_profit_price, event.timestamp, "tp_hit")
                    closed = True
            else:
                if price >= trade.stop_loss_price:
                    trade.close(trade.stop_loss_price, event.timestamp, "sl_hit")
                    closed = True
                elif price <= trade.take_profit_price:
                    trade.close(trade.take_profit_price, event.timestamp, "tp_hit")
                    closed = True

            if not closed:
                elapsed = (event.timestamp - trade.entry_timestamp).total_seconds()
                if elapsed >= self.risk.config.trade_timeout_seconds:
                    trade.close(price, event.timestamp, "timeout")
                    closed = True

            if closed:
                self._settle_closed_trade(trade)
            else:
                still_open.append(trade)

        self.portfolio.open_trades = still_open

    def _settle_closed_trade(self, trade: SimulatedTrade) -> None:
        assert trade.pnl_sol is not None

        self.portfolio.capital_sol += trade.size_sol + trade.pnl_sol - self.fee_sol
        self.portfolio.update_peak()

        if trade.pnl_sol < 0:
            self.portfolio.daily_loss_sol += trade.pnl_sol

        self.portfolio.closed_trades.append(trade)

        self.audit.record_trade_close(
            trade_id    = trade.id,
            token_mint  = trade.signal.token_mint,
            exit_price  = trade.exit_price,
            pnl_sol     = trade.pnl_sol,
            exit_reason = trade.exit_reason,
        )
        log.debug(
            "CLOSE %s | id=%s | pnl=%+.4f | reason=%-12s | cap=%.4f",
            trade.signal.token_mint[:8], trade.id,
            trade.pnl_sol, trade.exit_reason, self.portfolio.capital_sol,
        )

    def _force_close_all(self, reason: str) -> None:
        for trade in self.portfolio.open_trades:
            trade.close(trade.entry_price, datetime.now(timezone.utc), reason)
            self._settle_closed_trade(trade)
            log.warning("Force-closed open position %s (%s).", trade.id, reason)
        self.portfolio.open_trades = []


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    from engine.backtesting.data_loader import CSVDataLoader, SQLiteDataLoader

    parser = argparse.ArgumentParser(description="Alpha Engine Backtester")
    parser.add_argument("--source",  choices=["sqlite", "csv"], default="sqlite")
    parser.add_argument("--path",    required=True, help="Path to DB or CSV file")
    parser.add_argument("--capital", type=float, default=10.0, help="Starting capital in SOL")
    parser.add_argument("--start",   default=None, help="ISO 8601 start filter")
    parser.add_argument("--end",     default=None, help="ISO 8601 end filter")
    args = parser.parse_args()

    loader = (
        SQLiteDataLoader(args.path, start_time=args.start, end_time=args.end)
        if args.source == "sqlite"
        else CSVDataLoader(args.path)
    )

    harness = BacktestHarness(
        classifier  = null_classifier,   # ← swap in your real classifier
        risk_config = RiskConfig(initial_capital_sol=args.capital),
    )
    harness.run(loader)
