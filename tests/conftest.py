"""Shared pytest fixtures."""
from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
from unittest.mock import MagicMock

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def ohlcv_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 50
    idx = pd.date_range("2024-01-01 00:00", periods=n, freq="15min", tz="UTC")
    close = 50000 + np.cumsum(rng.normal(0, 50, n))
    high = close + rng.uniform(10, 100, n)
    low = close - rng.uniform(10, 100, n)
    open_ = close + rng.normal(0, 20, n)
    vol = rng.integers(100, 1000, n).astype(float)
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": vol},
        index=idx,
    )


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    db_file = tmp_path / "test_trades.db"
    monkeypatch.setenv("DB_PATH", str(db_file))

    import db.database as db_mod
    from sqlalchemy import create_engine

    new_engine = create_engine(
        f"sqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    monkeypatch.setattr(db_mod, "engine", new_engine)
    db_mod.metadata.create_all(new_engine)
    yield db_file


@pytest.fixture
def risk_manager(tmp_db, monkeypatch):
    monkeypatch.setattr("risk.manager.get_starting_equity", lambda d: None)
    monkeypatch.setattr("risk.manager.set_starting_equity", lambda d, v: None)
    from risk.manager import RiskManager
    return RiskManager(portfolio_value=10_000.0)


@pytest.fixture
def mock_exchange(monkeypatch):
    """Mock ccxt exchange for offline broker/fetcher tests."""
    exchange = MagicMock()
    exchange.markets = {"BTC/USDT": {"symbol": "BTC/USDT"}}
    exchange.amount_to_precision = lambda sym, amt: round(float(amt), 6)
    exchange.price_to_precision = lambda sym, price: round(float(price), 2)
    exchange.fetch_balance.return_value = {
        "free": {"USDT": 10000.0},
        "total": {"USDT": 10000.0},
    }
    exchange.fetch_ticker.return_value = {
        "bid": 50000.0,
        "ask": 50010.0,
        "last": 50005.0,
        "quoteVolume": 50_000_000,
        "percentage": 1.5,
    }
    exchange.fetch_ohlcv.return_value = [
        [1704067200000 + i * 900000, 50000, 50100, 49900, 50050, 100]
        for i in range(100)
    ]
    return exchange
