"""
Alpha Engine — Metrics Engine
================================
Computes a full PerformanceReport from a completed PortfolioState.

Metrics produced
----------------
  Equity curve          — capital over time (for plotting)
  Total PnL / ROI       — absolute and percentage
  Win rate              — wins / total closed trades
  Profit factor         — gross wins / gross losses
  Sharpe ratio          — annualised, assuming ~252 trading "days"
  Max drawdown          — worst peak-to-trough in percentage
  Average win / loss    — mean PnL of winning and losing trades
  Win/loss ratio        — avg win / avg loss
  Expectancy per trade  — (win_rate * avg_win) - (loss_rate * avg_loss)
  Trade duration stats  — avg, min, max time in position
  Exit reason breakdown — tp_hit / sl_hit / timeout / end_of_data counts
  Conviction calibration — does higher conviction produce better trades?
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engine.backtesting.harness import PortfolioState, SimulatedTrade


# ──────────────────────────────────────────────────────────────────────────────
# Output types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TradeStats:
    total_trades:      int
    winning_trades:    int
    losing_trades:     int
    breakeven_trades:  int
    win_rate_pct:      float
    avg_win_sol:       float
    avg_loss_sol:      float
    win_loss_ratio:    float
    profit_factor:     float
    expectancy_sol:    float

    exit_tp:       int   = 0
    exit_sl:       int   = 0
    exit_timeout:  int   = 0
    exit_eod:      int   = 0

    avg_duration_s: float = 0.0
    min_duration_s: float = 0.0
    max_duration_s: float = 0.0


@dataclass
class RiskStats:
    max_drawdown_pct:     float
    max_drawdown_sol:     float
    sharpe_ratio:         float
    avg_risk_per_trade:   float
    circuit_breaker_hit:  bool


@dataclass
class ConvictionBucket:
    label:          str
    trade_count:    int
    win_rate_pct:   float
    avg_pnl_sol:    float
    total_pnl_sol:  float


@dataclass
class PerformanceReport:
    initial_capital_sol: float
    final_capital_sol:   float
    total_pnl_sol:       float
    roi_pct:             float

    trade_stats: TradeStats
    risk_stats:  RiskStats

    conviction_buckets: list[ConvictionBucket] = field(default_factory=list)
    equity_curve:       list[tuple[str, float]] = field(default_factory=list)

    def print_summary(self) -> None:
        ts = self.trade_stats
        rs = self.risk_stats
        print("\n" + "=" * 60)
        print("  ALPHA ENGINE — BACKTEST REPORT")
        print("=" * 60)
        print(f"  Capital          {self.initial_capital_sol:.4f} → {self.final_capital_sol:.4f} SOL")
        print(f"  Total PnL        {self.total_pnl_sol:+.4f} SOL  ({self.roi_pct:+.1f}%)")
        print("-" * 60)
        print(f"  Trades           {ts.total_trades}  (W:{ts.winning_trades}  L:{ts.losing_trades}  B:{ts.breakeven_trades})")
        print(f"  Win Rate         {ts.win_rate_pct:.1f}%")
        print(f"  Profit Factor    {ts.profit_factor:.2f}  (need > 1.5)")
        print(f"  Sharpe Ratio     {rs.sharpe_ratio:.2f}  (need > 1.0)")
        print(f"  Expectancy       {ts.expectancy_sol:+.4f} SOL / trade")
        print(f"  Max Drawdown     {rs.max_drawdown_pct:.1f}%  ({rs.max_drawdown_sol:.4f} SOL)")
        print("-" * 60)
        print(f"  Avg Win          {ts.avg_win_sol:+.4f} SOL")
        print(f"  Avg Loss         {ts.avg_loss_sol:+.4f} SOL")
        print(f"  Win/Loss Ratio   {ts.win_loss_ratio:.2f}")
        print("-" * 60)
        print(f"  TP hits          {ts.exit_tp}   SL hits: {ts.exit_sl}   Timeouts: {ts.exit_timeout}")
        print(f"  Avg Duration     {ts.avg_duration_s:.0f}s  (min:{ts.min_duration_s:.0f}s  max:{ts.max_duration_s:.0f}s)")
        if self.conviction_buckets:
            print("-" * 60)
            print("  Conviction Calibration:")
            for b in self.conviction_buckets:
                print(f"    {b.label:<12} trades={b.trade_count:>3}  wr={b.win_rate_pct:>5.1f}%  avg={b.avg_pnl_sol:>+.4f} SOL")
        print("=" * 60 + "\n")


# ──────────────────────────────────────────────────────────────────────────────
# Metrics engine
# ──────────────────────────────────────────────────────────────────────────────

class MetricsEngine:
    """Stateless calculator. Call `compute(portfolio)` after a backtest run."""

    TRADING_PERIODS_PER_YEAR = 252 * 24 * 60   # minute-level granularity

    def compute(self, portfolio: "PortfolioState") -> PerformanceReport:
        trades = portfolio.closed_trades
        total  = len(trades)

        if total == 0:
            return self._empty_report(portfolio)

        wins   = [t for t in trades if t.pnl_sol and t.pnl_sol > 0.0001]
        losses = [t for t in trades if t.pnl_sol and t.pnl_sol < -0.0001]
        beven  = [t for t in trades if t not in wins and t not in losses]

        gross_win  = sum(t.pnl_sol for t in wins)   if wins   else 0.0
        gross_loss = sum(abs(t.pnl_sol) for t in losses) if losses else 1e-9

        avg_win  = gross_win  / len(wins)   if wins   else 0.0
        avg_loss = -gross_loss / len(losses) if losses else 0.0

        win_rate      = len(wins) / total * 100
        loss_rate     = len(losses) / total
        profit_factor = gross_win / gross_loss
        win_loss_ratio = avg_win / abs(avg_loss) if avg_loss != 0 else float("inf")
        expectancy     = (win_rate / 100 * avg_win) + (loss_rate * avg_loss)

        durations = []
        for t in trades:
            if t.entry_timestamp and t.exit_timestamp:
                durations.append((t.exit_timestamp - t.entry_timestamp).total_seconds())

        avg_dur = statistics.mean(durations) if durations else 0.0
        min_dur = min(durations)             if durations else 0.0
        max_dur = max(durations)             if durations else 0.0

        exit_counts = {"tp_hit": 0, "sl_hit": 0, "timeout": 0, "end_of_data": 0}
        for t in trades:
            if t.exit_reason in exit_counts:
                exit_counts[t.exit_reason] += 1

        trade_stats = TradeStats(
            total_trades     = total,
            winning_trades   = len(wins),
            losing_trades    = len(losses),
            breakeven_trades = len(beven),
            win_rate_pct     = win_rate,
            avg_win_sol      = avg_win,
            avg_loss_sol     = avg_loss,
            win_loss_ratio   = win_loss_ratio,
            profit_factor    = profit_factor,
            expectancy_sol   = expectancy,
            exit_tp          = exit_counts["tp_hit"],
            exit_sl          = exit_counts["sl_hit"],
            exit_timeout     = exit_counts["timeout"],
            exit_eod         = exit_counts["end_of_data"],
            avg_duration_s   = avg_dur,
            min_duration_s   = min_dur,
            max_duration_s   = max_dur,
        )

        equity_curve   = self._build_equity_curve(portfolio.initial_capital_sol, trades)
        max_dd_pct, max_dd_sol = self._max_drawdown(equity_curve)
        sharpe         = self._sharpe(equity_curve)

        risk_stats = RiskStats(
            max_drawdown_pct   = max_dd_pct,
            max_drawdown_sol   = max_dd_sol,
            sharpe_ratio       = sharpe,
            avg_risk_per_trade = sum(t.size_sol for t in trades) / total,
            circuit_breaker_hit = portfolio.circuit_breaker_active,
        )

        conviction_buckets = self._conviction_buckets(trades)
        curve_points = [(t[0].isoformat(), t[1]) for t in equity_curve]

        return PerformanceReport(
            initial_capital_sol = portfolio.initial_capital_sol,
            final_capital_sol   = portfolio.capital_sol,
            total_pnl_sol       = portfolio.capital_sol - portfolio.initial_capital_sol,
            roi_pct             = (portfolio.capital_sol - portfolio.initial_capital_sol)
                                  / portfolio.initial_capital_sol * 100,
            trade_stats         = trade_stats,
            risk_stats          = risk_stats,
            conviction_buckets  = conviction_buckets,
            equity_curve        = curve_points,
        )

    def _build_equity_curve(self, initial: float, trades: list) -> list[tuple]:
        sorted_trades = sorted(
            [t for t in trades if t.exit_timestamp and t.pnl_sol is not None],
            key=lambda t: t.exit_timestamp,
        )
        curve = [(trades[0].entry_timestamp, initial)] if trades else []
        running = initial
        for t in sorted_trades:
            running += t.pnl_sol
            curve.append((t.exit_timestamp, running))
        return curve

    def _max_drawdown(self, curve: list[tuple]) -> tuple[float, float]:
        if not curve:
            return 0.0, 0.0
        peak = curve[0][1]
        max_dd_sol = 0.0
        for _, val in curve:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd_sol:
                max_dd_sol = dd
        max_dd_pct = max_dd_sol / curve[0][1] * 100 if curve[0][1] > 0 else 0.0
        return max_dd_pct, max_dd_sol

    def _sharpe(self, curve: list[tuple]) -> float:
        if len(curve) < 2:
            return 0.0
        returns = []
        for i in range(1, len(curve)):
            prev = curve[i - 1][1]
            curr = curve[i][1]
            if prev > 0:
                returns.append((curr - prev) / prev)
        if len(returns) < 2:
            return 0.0
        mean_r = statistics.mean(returns)
        std_r  = statistics.stdev(returns)
        if std_r == 0:
            return 0.0
        return round(mean_r / std_r * math.sqrt(self.TRADING_PERIODS_PER_YEAR), 3)

    def _conviction_buckets(self, trades: list) -> list[ConvictionBucket]:
        bands = [(0, 50, "0-50"), (50, 70, "50-70"), (70, 85, "70-85"), (85, 90, "85-90"), (90, 101, "90-100")]
        buckets = []
        for lo, hi, label in bands:
            band_trades = [
                t for t in trades
                if lo <= t.signal.conviction_score < hi and t.pnl_sol is not None
            ]
            if not band_trades:
                continue
            wins_in_band = [t for t in band_trades if t.pnl_sol > 0.0001]
            total_pnl    = sum(t.pnl_sol for t in band_trades)
            buckets.append(ConvictionBucket(
                label         = label,
                trade_count   = len(band_trades),
                win_rate_pct  = len(wins_in_band) / len(band_trades) * 100,
                avg_pnl_sol   = total_pnl / len(band_trades),
                total_pnl_sol = total_pnl,
            ))
        return buckets

    def _empty_report(self, portfolio: "PortfolioState") -> PerformanceReport:
        return PerformanceReport(
            initial_capital_sol = portfolio.initial_capital_sol,
            final_capital_sol   = portfolio.capital_sol,
            total_pnl_sol       = 0.0,
            roi_pct             = 0.0,
            trade_stats = TradeStats(0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
            risk_stats  = RiskStats(0.0, 0.0, 0.0, 0.0, False),
        )
