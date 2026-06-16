#!/usr/bin/env python3
"""5-step preflight before going live."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.settings import (
    ANTHROPIC_API_KEY,
    EXCHANGE_API_KEY,
    EXCHANGE_SECRET,
    EXCHANGE_SANDBOX,
    MAX_DAILY_LOSS_PCT,
    MAX_OPEN_POSITIONS,
    MAX_PORTFOLIO_RISK_PCT,
    TRADING_MODE,
)
from data.fetcher import CryptoDataFetcher
from execution.broker import CryptoBroker
from logs.logger import get_logger

logger = get_logger("go_live")


def _step(n: int, title: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    logger.info(f"Step {n}: {title} — {status}" + (f" ({detail})" if detail else ""))
    return ok


def main() -> int:
    logger.info("=== Go-Live Preflight ===")
    all_ok = True

    all_ok &= _step(
        1, "TRADING_MODE=live",
        TRADING_MODE == "live",
        f"current={TRADING_MODE}",
    )
    all_ok &= _step(
        2, "API keys configured",
        bool(EXCHANGE_API_KEY and EXCHANGE_SECRET and ANTHROPIC_API_KEY),
    )
    all_ok &= _step(
        3, "Not on sandbox/testnet",
        not EXCHANGE_SANDBOX,
        "EXCHANGE_SANDBOX must be false for live",
    )

    fetcher = CryptoDataFetcher()
    bars_ok = False
    if fetcher.exchange:
        df = fetcher.get_bars("BTC/USDT", limit=50)
        bars_ok = not df.empty
    all_ok &= _step(4, "Market data fetch", bars_ok)

    broker = CryptoBroker(dry_run=True)
    acct_ok = False
    try:
        acct = broker.get_account()
        acct_ok = float(acct.get("equity", 0)) > 0
    except Exception:
        pass
    all_ok &= _step(5, "Account access", acct_ok)

    logger.info(
        f"Risk params: risk/trade={MAX_PORTFOLIO_RISK_PCT:.1%}  "
        f"max_positions={MAX_OPEN_POSITIONS}  "
        f"daily_loss_halt={MAX_DAILY_LOSS_PCT:.1%}"
    )

    if all_ok:
        logger.info("All preflight checks passed. Run: python main.py --mode live")
        return 0

    logger.error("Preflight FAILED — fix issues before going live.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
