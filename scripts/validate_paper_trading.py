#!/usr/bin/env python3
"""Paper trading validation helper.

Run the bot on exchange testnet for 2+ weeks, then use this script to
review signal quality and readiness for live trading.

Usage:
    python scripts/validate_paper_trading.py
"""
from __future__ import annotations

import os
import sys
from datetime import date, timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from config.settings import EXCHANGE_SANDBOX, TRADING_MODE
from db.database import db_compute_all_time_stats, db_get_daily_stats, get_recent_signals
from logs.logger import get_logger

logger = get_logger("validate_paper")


def main() -> int:
    logger.info("=== Paper Trading Validation Report ===")

    if TRADING_MODE != "paper":
        logger.warning(f"TRADING_MODE={TRADING_MODE} (expected 'paper')")
    if not EXCHANGE_SANDBOX:
        logger.warning("EXCHANGE_SANDBOX=false — you may be on live exchange keys")

    stats = db_compute_all_time_stats()
    logger.info(
        f"All-time: trades={stats['total_trades']}  "
        f"win_rate={stats['win_rate']:.1%}  "
        f"total_pnl=${stats['total_pnl']:+,.2f}  "
        f"max_dd={stats['max_drawdown_pct']:.2%}"
    )

    days_with_data = 0
    for i in range(14):
        d = date.today() - timedelta(days=i)
        row = db_get_daily_stats(d)
        if row:
            days_with_data += 1

    logger.info(f"Days with daily stats (last 14): {days_with_data}/14")
    if days_with_data < 14:
        logger.warning(
            "Recommend running on testnet for at least 14 days before going live."
        )

    signals = get_recent_signals(limit=50)
    executed = sum(1 for s in signals if s.get("was_executed"))
    logger.info(
        f"Recent signals: {len(signals)} total, {executed} executed"
    )

    ready = (
        stats["total_trades"] >= 10
        and days_with_data >= 14
        and stats["max_drawdown_pct"] < 0.15
    )

    if ready:
        logger.info("VALIDATION: Criteria met — review results manually before live.")
        return 0

    logger.warning(
        "VALIDATION: Not ready for live. Continue paper trading on testnet."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
