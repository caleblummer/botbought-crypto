#!/usr/bin/env python3
"""Emergency: close all positions and cancel all orders."""
from __future__ import annotations

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from execution.broker import BrokerError, CryptoBroker
from logs.logger import get_logger

logger = get_logger("close_all")


def main() -> int:
    logger.warning("EMERGENCY CLOSE — flattening all positions")
    broker = CryptoBroker(dry_run=False)
    try:
        n = broker.cancel_all_orders()
        logger.info(f"Cancelled {n} open order(s)")
        broker.close_all_positions_eod()
        logger.info("All positions closed.")
        return 0
    except BrokerError as exc:
        logger.error(f"Emergency close failed: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
