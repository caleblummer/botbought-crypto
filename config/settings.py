import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ── Exchange (ccxt) ─────────────────────────────────────────────────────────
EXCHANGE_ID: str = os.getenv("EXCHANGE_ID", "binance")
EXCHANGE_API_KEY: str = os.getenv("EXCHANGE_API_KEY", "")
EXCHANGE_SECRET: str = os.getenv("EXCHANGE_SECRET", "")
EXCHANGE_SANDBOX: bool = os.getenv("EXCHANGE_SANDBOX", "true").lower() in (
    "1", "true", "yes",
)

# ── Anthropic ─────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL: str = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-6")

# ── Trading mode ──────────────────────────────────────────────────────────────
TRADING_MODE: str = os.getenv("TRADING_MODE", "paper").lower()
if TRADING_MODE not in ("paper", "live"):
    print(f"WARNING: Unknown TRADING_MODE={TRADING_MODE!r}; defaulting to 'paper'")
    TRADING_MODE = "paper"

MARKET_TYPE: str = os.getenv("MARKET_TYPE", "spot").lower()
if MARKET_TYPE not in ("spot", "futures"):
    print(f"WARNING: Unknown MARKET_TYPE={MARKET_TYPE!r}; defaulting to 'spot'")
    MARKET_TYPE = "spot"

# ── Risk management ───────────────────────────────────────────────────────────
MAX_PORTFOLIO_RISK_PCT: float = float(os.getenv("MAX_PORTFOLIO_RISK_PCT", "0.02"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "5"))
MAX_DAILY_LOSS_PCT: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "0.05"))
MIN_TRADE_CAPITAL: float = float(os.getenv("MIN_TRADE_CAPITAL", "10.0"))

# ── Scheduling (24/7 crypto) ──────────────────────────────────────────────────
SCAN_INTERVAL_MINUTES: int = int(os.getenv("SCAN_INTERVAL_MINUTES", "5"))
BAR_TIMEFRAME: str = os.getenv("BAR_TIMEFRAME", "15m")
# Optional UTC time (HH:MM) to close all positions daily; empty = hold overnight
DAILY_CLOSE_UTC: str = os.getenv("DAILY_CLOSE_UTC", "").strip()

# ── Crypto filters ────────────────────────────────────────────────────────────
MIN_24H_VOLUME_USD: float = float(os.getenv("MIN_24H_VOLUME_USD", "10000000"))

# ── Database ──────────────────────────────────────────────────────────────────
DB_PATH: str = os.getenv("DB_PATH", "db/trades.db")

# ── Technical indicator parameters ───────────────────────────────────────────
RSI_PERIOD: int = 14
MACD_FAST: int = 12
MACD_SLOW: int = 26
MACD_SIGNAL: int = 9
BB_PERIOD: int = 20
BB_STD: int = 2
ATR_PERIOD: int = 14


def require_live_confirmation() -> None:
    """Block startup unless the user explicitly confirms live-trading intent."""
    if TRADING_MODE != "live":
        return

    banner = """
╔══════════════════════════════════════════════════════════════════╗
║                  ⚠️   LIVE TRADING MODE   ⚠️                     ║
║                                                                  ║
║  TRADING_MODE=live is set. This bot will place REAL ORDERS      ║
║  with REAL MONEY on your crypto exchange account.                ║
║                                                                  ║
║  • Losses are permanent.                                         ║
║  • There is no undo for executed trades.                         ║
║  • Ensure your risk parameters are correct before continuing.    ║
║                                                                  ║
║  Type  CONFIRM LIVE  to proceed, or anything else to abort.      ║
╚══════════════════════════════════════════════════════════════════╝
"""
    print(banner, flush=True)

    try:
        response = input(">>> ").strip()
    except (EOFError, KeyboardInterrupt):
        response = ""

    if response != "CONFIRM LIVE":
        print("Aborted. Set TRADING_MODE=paper to run safely.")
        sys.exit(1)

    print("Live trading confirmed. Proceeding with caution.\n")
