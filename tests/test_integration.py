"""Integration tests for the Crypto AI Day Trader bot.

All tests run offline — no real exchange or Anthropic API calls.

Run with:
    pytest tests/test_integration.py -v
"""
from __future__ import annotations

import json
import math
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

_FIXTURES = Path(__file__).parent / "fixtures"


class TestIndicatorCalculation:
    def test_loads_csv_and_computes_indicators(self):
        from data.indicators import compute_indicators

        df_raw = pd.read_csv(
            _FIXTURES / "sample_ohlcv.csv",
            index_col="datetime",
            parse_dates=True,
        )
        df = compute_indicators(df_raw)
        for col in ["rsi_14", "macd", "atr_14", "volume_ratio", "vwap"]:
            assert col in df.columns

    def test_no_nan_in_last_five_rows(self, ohlcv_df):
        from data.indicators import compute_indicators

        df = compute_indicators(ohlcv_df)
        tail = df.tail(5)
        nan_counts = tail.select_dtypes(include="number").isna().sum()
        assert not any(nan_counts[nan_counts > 0])

    def test_rsi_in_valid_range(self, ohlcv_df):
        from data.indicators import compute_indicators

        df = compute_indicators(ohlcv_df)
        rsi = df["rsi_14"].dropna()
        assert (rsi >= 0).all()
        assert (rsi <= 100).all()

    def test_signal_summary_returns_expected_keys(self, ohlcv_df):
        from data.indicators import compute_indicators, get_signal_summary

        df = compute_indicators(ohlcv_df)
        summary = get_signal_summary(df)
        for key in ["price", "rsi_14", "macd", "atr_14", "summary_text"]:
            assert key in summary


class TestRiskPositionSize:
    def test_standard_position_size(self, risk_manager):
        qty = risk_manager.calculate_position_size(10_000.0, 50000.0, 49000.0)
        assert qty > 0

    def test_zero_size_when_stop_equals_entry(self, risk_manager):
        qty = risk_manager.calculate_position_size(10_000.0, 50000.0, 50000.0)
        assert qty == 0.0

    def test_20pct_portfolio_cap(self, risk_manager):
        qty = risk_manager.calculate_position_size(10_000.0, 50000.0, 49999.0)
        max_allowed = 10_000.0 * 0.20 / 50000.0
        assert qty <= max_allowed


class TestDailyLossHalt:
    def test_halt_triggers_at_limit(self, risk_manager):
        start = 10_000.0
        current = start * (1 - 0.051)
        assert risk_manager.check_daily_loss_limit(start, current) is True

    def test_no_halt_below_limit(self, risk_manager):
        assert risk_manager.check_daily_loss_limit(10_000.0, 9_600.0) is False


class TestSignalParseError:
    def _make_generator(self, raw_response: str):
        from strategy.ai_signal import AISignalGenerator

        gen = AISignalGenerator.__new__(AISignalGenerator)
        gen._cache = {}
        mock_block = MagicMock()
        mock_block.type = "text"
        mock_block.text = raw_response
        mock_response = MagicMock()
        mock_response.content = [mock_block]
        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response
        gen._client = mock_client
        return gen

    def test_malformed_json_returns_hold(self):
        gen = self._make_generator("{not valid json!!}")
        result = gen.analyze("BTC/USDT", {"price": 50000.0}, "summary")
        assert result["signal"] == "HOLD"

    def test_valid_buy_signal_passes_through(self):
        payload = json.dumps({
            "signal": "BUY",
            "confidence": 0.82,
            "reasoning": "RSI oversold with MACD crossover",
            "suggested_entry": 50000.0,
            "suggested_stop_loss": 49000.0,
            "suggested_take_profit": 52000.0,
            "risk_level": "MEDIUM",
        })
        gen = self._make_generator(payload)
        result = gen.analyze("BTC/USDT", {"price": 50000.0}, "")
        assert result["signal"] == "BUY"


class TestPreFilterBlocks:
    def _filter(self):
        from strategy.rules import RuleFilter
        return RuleFilter()

    def _df(self, n=20):
        close = pd.Series([50000.0 + (50 if i % 2 == 0 else -50) for i in range(n)])
        return pd.DataFrame({
            "open": close - 10,
            "high": close + 50,
            "low": close - 50,
            "close": close,
            "volume": [1000.0] * n,
        })

    def test_blocks_low_volume(self):
        rf = self._filter()
        ind = {
            "price": 50000.0,
            "volume_ratio": 0.3,
            "atr_pct_price": 1.0,
            "quote_volume_24h": 50_000_000,
        }
        ok, reason = rf.pre_filter("BTC/USDT", self._df(), ind)
        assert ok is False

    def test_passes_clean_pair(self):
        rf = self._filter()
        ind = {
            "price": 50000.0,
            "volume_ratio": 1.2,
            "atr_pct_price": 1.0,
            "quote_volume_24h": 50_000_000,
        }
        ok, _ = rf.pre_filter("BTC/USDT", self._df(), ind)
        assert ok is True


class TestBracketOrderDryRun:
    @pytest.fixture
    def broker(self, monkeypatch):
        monkeypatch.setenv("EXCHANGE_API_KEY", "")
        from execution.broker import CryptoBroker
        return CryptoBroker(dry_run=True)

    def test_returns_dry_order_dict(self, broker):
        result = broker.submit_bracket_order(
            symbol="BTC/USDT",
            qty=0.01,
            side="buy",
            entry_price=50000.0,
            stop_loss=49000.0,
            take_profit=52000.0,
        )
        assert result.get("status") == "dry_run"
        assert str(result.get("order_id", "")).startswith("dry-")

    def test_raises_on_zero_qty(self, broker):
        from execution.broker import BrokerError
        with pytest.raises(BrokerError):
            broker.submit_bracket_order(
                symbol="BTC/USDT", qty=0, side="buy",
                entry_price=50000.0, stop_loss=49000.0, take_profit=52000.0,
            )


class TestDatabaseWriteRead:
    def test_insert_and_retrieve_trade(self, tmp_db):
        from db.database import db_get_open_trades, db_insert_trade

        trade_id = db_insert_trade({
            "symbol": "BTC/USDT",
            "side": "buy",
            "qty": 0.01,
            "entry_price": 50000.0,
            "stop_loss": 49000.0,
            "take_profit": 52000.0,
            "ai_signal_confidence": 0.82,
            "ai_reasoning": "test",
            "order_id": "test-001",
        })
        assert trade_id > 0
        rows = db_get_open_trades()
        assert rows[0]["symbol"] == "BTC/USDT"
        assert rows[0]["qty"] == pytest.approx(0.01)

    def test_close_trade_updates_status(self, tmp_db):
        from db.database import db_close_trade, db_get_open_trades, db_insert_trade

        tid = db_insert_trade({
            "symbol": "ETH/USDT",
            "side": "buy",
            "qty": 0.1,
            "entry_price": 3000.0,
        })
        db_close_trade(tid, 3100.0, datetime.utcnow(), 10.0, 0.03)
        assert len(db_get_open_trades()) == 0
