# SOL Trader Engine - Python Backend

Autonomous Solana trading engine using SMC (Smart Money Concepts) with AGI-simulated reasoning.

## Modules

- `listener.py` — Helius WebSocket listener for new Pump.fun mint detection
- `rug_checks.py` — Multi-layer rug detection (metadata, liquidity, clusters, Arkham)
- `entry_strategies.py` — TWAP stealth entry with survival guard
- `risk_management.py` — Trailing stop, profit ladder (5x/70-30 split), moonbag management
- `regime_analysis.py` — RSI/EMA regime detection (BREAKOUT / OVERSOLD / STABLE)

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# Fill in your keys in .env
python -m engine.listener
```

## Architecture

```
Helius WSS → filter_worker → rug_check_worker → executioner
                                                      ↓
                                             entry_strategies (TWAP)
                                                      ↓
                                             risk_management (guard)
```
