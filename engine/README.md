# Alpha Engine — Autonomous Solana Trading Bot

Slow-compounding, self-aware Solana trading engine using Smart Money Concepts (SMC)
with AGI-simulated regime intelligence, rug detection, and a full backtesting harness.

---

## Package Layout

```
engine/
├── main.py                     ← Compounding loop entry point
├── intelligence.py             ← Regime intelligence + self-adjusting AI params
├── trade_logger.py             ← CSV trade log (devnet vs mainnet comparison)
├── backtest_harness.py         ← Backward-compat shim → engine.backtesting.*
│
├── infrastructure/
│   ├── logger.py               ← JSON rotating logger + AuditLogger (append-only)
│   ├── monitor.py              ← HeartbeatMonitor: RPC latency, wallet drift, Telegram
│   └── priority_fee_oracle.py ← Async fee oracle with SQLite Rust bridge
│
├── backtesting/
│   ├── harness.py              ← Event-driven backtest/paper/shadow runner
│   ├── data_loader.py          ← SQLite / CSV / live WebSocket data sources
│   └── metrics.py              ← Sharpe, drawdown, conviction calibration
│
├── listener.py                 ← Helius WebSocket: new mint detection
├── rug_checks.py               ← Multi-layer rug detection (metadata, liquidity, Arkham)
├── entry_strategies.py         ← TWAP stealth entry with survival guard
├── risk_management.py          ← Trailing stop, 5x ladder, moonbag management
├── regime_analysis.py          ← RSI/EMA regime: BREAKOUT / OVERSOLD / STABLE
├── wallet_tracker.py           ← On-chain wallet change detection
├── backtest_harness.py         ← Legacy shim
├── devnet_simulator.py         ← Devnet paper mode
├── requirements.txt
└── .env.example
```

---

## Quick Start

```bash
pip install -r engine/requirements.txt
cp engine/.env.example engine/.env
# Edit .env — add HELIUS_RPC_URL, SOLANA_WALLET_ADDRESS, etc.
python -m engine.main
```

---

## Backtesting

```bash
# Replay from SQLite audit DB
python -m engine.backtesting.harness --source sqlite --path data/alpha_engine.db --capital 10.0

# Replay from CSV export (Birdeye / Helius)
python -m engine.backtesting.harness --source csv --path data/ticks.csv --capital 10.0
```

Or in Python:

```python
from engine.backtesting.harness import BacktestHarness, RiskConfig
from engine.backtesting.data_loader import CSVDataLoader

harness = BacktestHarness(
    classifier  = my_classifier_fn,   # fn(MarketEvent) -> Optional[Signal]
    risk_config = RiskConfig(initial_capital_sol=10.0),
)
report = harness.run(CSVDataLoader("data/ticks.csv"))
report.print_summary()
```

---

## Infrastructure

### Structured Logger

```python
from engine.infrastructure.logger import get_logger, AuditLogger

log   = get_logger(__name__)
audit = AuditLogger()

log.info("Signal generated", extra={"mint": mint, "score": 87.3})
audit.record_trade_open(trade_id, mint, entry, size, sl, tp, score)
audit.record_trade_close(trade_id, mint, exit_price, pnl, "tp_hit")
```

Logs rotate at 10 MB (7 files max). Audit trail is append-only JSONL.

### Priority Fee Oracle

```python
import asyncio
from engine.infrastructure.priority_fee_oracle import PriorityFeeOracle

oracle = PriorityFeeOracle(rpc_url=os.getenv("HELIUS_RPC_URL"), db_path="data/fees.db")
rec    = await oracle.recommend(urgency="high")
print(rec)  # FeeRec[high] rec=150,000 µL/CU  p50=...  ~0.000030 SOL
```

### Heartbeat Monitor

```bash
python -m engine.infrastructure.monitor
```

Monitors: process PID, RPC latency, signal silence, circuit breaker, wallet drift.
Sends Telegram alerts. Auto-restarts dead bot process once, then escalates.

---

## VPS Deployment

```bash
# One-shot setup — Ubuntu 24.04 LTS required
sudo bash vps_setup.sh

# Edit /opt/alpha-engine/.env — add your real keys
sudo systemctl start alpha-bot.service
sudo systemctl start alpha-monitor.service

sudo journalctl -fu alpha-bot      # live bot logs
sudo journalctl -fu alpha-monitor  # live monitor logs
sudo ufw status verbose             # verify firewall
sudo fail2ban-client status sshd   # verify SSH ban policy
ls /opt/alpha-engine/backups/      # nightly SQLite backups
```

**What the script provisions (Ubuntu 24.04):**
- Python 3.12 venv, Node.js 20 LTS
- UFW firewall: default-deny inbound, SSH-only allowed
- fail2ban: SSH jail (5 failures → 1h ban)
- unattended-upgrades: auto security patches, no auto-reboot
- Two independent systemd services (bot crash cannot silence the monitor)
- logrotate: 30-day retention for `.log` and `.jsonl` files
- Nightly cron: SQLite `.backup` at 02:00 UTC, 30-day backup retention

---

## Environment Variables

| Variable                  | Description                                      |
|---------------------------|--------------------------------------------------|
| `HELIUS_RPC_URL`          | Helius mainnet RPC URL with API key              |
| `RPC_URL_FALLBACK`        | Backup RPC (e.g. public endpoint)                |
| `SOLANA_NETWORK`          | `devnet` or `mainnet-beta`                       |
| `SOLANA_WALLET_ADDRESS`   | Your wallet public key                           |
| `SOLANA_WALLET_PRIVATE_KEY` | Your wallet private key (keep secret!)         |
| `TELEGRAM_BOT_TOKEN`      | Bot token from @BotFather                        |
| `TELEGRAM_CHAT_ID`        | Your Telegram chat ID                            |
| `ALPHA_LOG_LEVEL`         | `DEBUG` / `INFO` / `WARNING` (default: `INFO`)   |
| `ALPHA_LOG_DIR`           | Log directory (default: `logs/`)                 |
| `ALPHA_DB_PATH`           | SQLite path (default: `data/alpha_engine.db`)    |
| `ALPHA_PID_FILE`          | PID file for monitor (default: `alpha_engine.pid`) |

---

## Slow-Compounding Safety Config

| Parameter              | Value | Meaning                                     |
|------------------------|-------|---------------------------------------------|
| `MAX_PER_TOKEN_PCT`    | 5%    | Max exposure per token                      |
| `MAX_CONCURRENT_TOKENS`| 2     | Never hold more than 2 positions at once    |
| `MAX_RISK_PCT`         | 25%   | Max total capital at risk simultaneously    |
| `base_risk_pct`        | 1%    | Base position size per trade                |
| `conviction_punt_pct`  | 5%    | Increased size for ≥90-score signals        |
| `stop_loss_pct`        | 5%    | Hard stop loss                              |
| `take_profit_pct`      | 15%   | Take profit target                          |
| `circuit_breaker`      | 30%   | Halt all trading on 30% drawdown            |
| `daily_loss_limit`     | 10%   | Halt trading if daily loss exceeds 10%      |
