#!/usr/bin/env python3
"""Run a single paper-trading scan cycle (dry-run)."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.database import init_db
from logs.logger import get_logger
from main import TradingBot

logger = get_logger("paper_trading_test")


def main() -> int:
    init_db()
    bot = TradingBot(dry_run=True)
    bot.startup()
    logger.info("Running single paper scan cycle...")
    bot.scan_and_trade()
    logger.info("Paper test cycle complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
