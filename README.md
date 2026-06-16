# Crypto AI Day Trader

An intraday crypto trading bot that combines rule-based technical analysis with Claude AI signals to scan a watchlist every few minutes (24/7), size positions with layered risk controls, and persist everything to SQLite for a live Streamlit dashboard.

> **Experimental software.** Automated trading carries significant financial risk. Start with exchange testnet (`EXCHANGE_SANDBOX=true`) and paper mode (`TRADING_MODE=paper`).

## How it works

Each scan cycle (every 5 minutes by default, 24/7):

1. **Fetch bars** — OHLCV via ccxt (`15m` default)
2. **Compute indicators** — RSI, MACD, Bollinger Bands, ATR, EMA, rolling 24h VWAP, volume ratio
3. **Pre-filter** — blocks illiquid pairs, wide spreads, high ATR, low 24h volume, exhaustion patterns
4. **AI signal** — Claude returns BUY/SELL/HOLD with confidence and suggested entry/stop/target
5. **Post-filter** — confidence ≥ 0.65, position limits
6. **Position sizing** — 2% portfolio risk per trade, ATR-based stops, 2:1 reward-to-risk
7. **Order submission** — limit entry + managed stop/take-profit via ccxt (dry-run in paper mode)
8. **Trailing stops** — tightened each cycle on profitable positions

## Prerequisites

| Requirement | Notes |
|---|---|
| **Python 3.11+** | Tested with 3.11–3.14 |
| **Exchange account** | Binance (default), or swap `EXCHANGE_ID` to `coinbase`, `kraken`, etc. |
| **Anthropic API key** | For Claude AI signals |
| **Testnet keys** | Use exchange sandbox for paper trading |

## Setup

```bash
cd botbought-crypto
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your testnet API keys and Anthropic key
```

## Validate with backtesting

```bash
python -m backtest.runner
python -m backtest.runner BTC/USDT 2025-01-01 2025-06-01
```

## Run tests

```bash
pytest tests/test_integration.py -v
```

## Paper trading

```bash
# Single dry-run scan cycle
python scripts/paper_trading_test.py

# Start the bot (24/7, dry-run)
python main.py --mode paper
```

## Dashboard

```bash
streamlit run dashboard/app.py
```

## Going live

Only after **2+ weeks** on testnet:

```bash
python scripts/validate_paper_trading.py   # check readiness
python scripts/go_live.py                  # preflight checks
# Set TRADING_MODE=live, EXCHANGE_SANDBOX=false in .env
python main.py --mode live
```

## Emergency stop

```bash
python scripts/close_all.py
```

## Configuration

Key `.env` variables:

| Variable | Default | Purpose |
|---|---|---|
| `EXCHANGE_ID` | `binance` | ccxt exchange |
| `EXCHANGE_SANDBOX` | `true` | Use testnet |
| `TRADING_MODE` | `paper` | `paper` or `live` |
| `MARKET_TYPE` | `spot` | `spot` (futures later) |
| `WATCHLIST` | BTC,ETH,SOL | Comma-separated pairs |
| `SCAN_INTERVAL_MINUTES` | `5` | Scan frequency |
| `DAILY_CLOSE_UTC` | (empty) | Optional daily close time e.g. `00:00` |

## Project structure

```
botbought-crypto/
├── main.py                 # Bot orchestrator (24/7 scheduler)
├── config/                 # Settings and watchlist
├── data/                   # ccxt fetcher + indicators
├── strategy/               # AI signals + rule filters
├── risk/                   # Position sizing + loss limits
├── execution/              # ccxt broker + portfolio
├── db/                     # SQLite persistence
├── dashboard/              # Streamlit UI
├── backtest/               # Historical validation
├── scripts/                # Paper test, go-live, emergency close
└── tests/                  # Offline integration tests
```

## Deployment (24/7)

Crypto never sleeps — deploy on a VPS with `systemd` or `supervisord`:

```bash
# Example systemd unit
python main.py --mode paper
streamlit run dashboard/app.py --server.port 8501
```

Restrict API keys to **trade-only** permissions (no withdrawal) and whitelist your VPS IP.

## Risk disclaimer

This bot is experimental. Past backtest or paper results do not guarantee future performance. Only trade capital you can afford to lose. Crypto markets are highly volatile and operate 24/7.
