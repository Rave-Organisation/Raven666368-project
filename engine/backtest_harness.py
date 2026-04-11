"""
Backward-compat shim — the real harness now lives in engine.backtesting.harness.
Import from there for all new code.
"""
from engine.backtesting.harness import BacktestHarness, RiskConfig, null_classifier, Signal, Side
from engine.backtesting.data_loader import SQLiteDataLoader, CSVDataLoader
from engine.backtesting.metrics import PerformanceReport

__all__ = [
    "BacktestHarness", "RiskConfig", "null_classifier", "Signal", "Side",
    "SQLiteDataLoader", "CSVDataLoader", "PerformanceReport",
]
