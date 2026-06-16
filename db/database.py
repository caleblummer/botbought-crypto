from __future__ import annotations

import math
import os
from datetime import date, datetime
from typing import Optional

from sqlalchemy import (
    Boolean, Column, Date, DateTime, Float, Integer,
    MetaData, String, Table, Text,
    create_engine, insert, select, update,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from config.settings import DB_PATH, MARKET_TYPE
from logs.logger import get_logger

logger = get_logger(__name__)

_db_dir = os.path.dirname(DB_PATH)
if _db_dir:
    os.makedirs(_db_dir, exist_ok=True)

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
)

metadata = MetaData()

trades = Table(
    "trades", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("symbol", String(32), nullable=False),
    Column("side", String(8)),
    Column("qty", Float),
    Column("entry_price", Float),
    Column("exit_price", Float),
    Column("stop_loss", Float),
    Column("take_profit", Float),
    Column("entry_time", DateTime),
    Column("exit_time", DateTime),
    Column("pnl", Float),
    Column("pnl_pct", Float),
    Column("status", String(16)),
    Column("market_type", String(16), default=MARKET_TYPE),
    Column("ai_signal_confidence", Float),
    Column("ai_reasoning", Text),
    Column("order_id", String(64)),
)

daily_stats = Table(
    "daily_stats", metadata,
    Column("date", Date, primary_key=True),
    Column("starting_equity", Float),
    Column("ending_equity", Float),
    Column("trades_taken", Integer),
    Column("trades_won", Integer),
    Column("trades_lost", Integer),
    Column("total_pnl", Float),
    Column("max_drawdown_pct", Float),
)

signals_log = Table(
    "signals_log", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", DateTime),
    Column("symbol", String(32)),
    Column("signal", String(8)),
    Column("confidence", Float),
    Column("reasoning", Text),
    Column("rule_score", Float),
    Column("was_executed", Boolean),
    Column("filter_block_reason", Text),
)

daily_equity = Table(
    "daily_equity", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("trade_date", Date, nullable=False, unique=True),
    Column("starting_equity", Float, nullable=False),
)

portfolio_snapshots = Table(
    "portfolio_snapshots", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("timestamp", DateTime),
    Column("equity", Float),
    Column("cash", Float),
    Column("buying_power", Float),
    Column("open_positions", Integer),
)


def init_db() -> None:
    metadata.create_all(engine)
    logger.info(f"Database ready: {DB_PATH}")


def db_insert_trade(data: dict) -> int:
    row = {
        "symbol": data["symbol"],
        "side": data.get("side"),
        "qty": data.get("qty"),
        "entry_price": data.get("entry_price"),
        "stop_loss": data.get("stop_loss"),
        "take_profit": data.get("take_profit"),
        "entry_time": data.get("entry_time", datetime.utcnow()),
        "status": "open",
        "market_type": data.get("market_type", MARKET_TYPE),
        "ai_signal_confidence": data.get("ai_signal_confidence"),
        "ai_reasoning": data.get("ai_reasoning"),
        "order_id": data.get("order_id"),
    }
    with engine.begin() as conn:
        result = conn.execute(insert(trades).values(**row))
        return result.lastrowid


def db_close_trade(
    trade_id: int,
    exit_price: float,
    exit_time: datetime,
    pnl: float,
    pnl_pct: float,
    status: str = "closed",
) -> None:
    with engine.begin() as conn:
        conn.execute(
            update(trades)
            .where(trades.c.id == trade_id)
            .values(
                exit_price=exit_price,
                exit_time=exit_time,
                pnl=round(pnl, 4),
                pnl_pct=round(pnl_pct, 6),
                status=status,
            )
        )


def db_get_open_trades() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(trades).where(trades.c.status == "open")
            .order_by(trades.c.entry_time.asc())
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def db_get_today_trades() -> list[dict]:
    today_start = datetime.combine(date.today(), datetime.min.time())
    with engine.connect() as conn:
        rows = conn.execute(
            select(trades)
            .where(trades.c.entry_time >= today_start)
            .order_by(trades.c.entry_time.asc())
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def db_get_all_closed_trades() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(trades.c.pnl, trades.c.pnl_pct, trades.c.entry_time)
            .where(trades.c.status == "closed")
            .order_by(trades.c.entry_time.asc())
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def db_upsert_daily_stats(data: dict) -> None:
    stmt = sqlite_insert(daily_stats).values(**data)
    stmt = stmt.on_conflict_do_update(
        index_elements=["date"],
        set_={c: stmt.excluded[c] for c in data if c != "date"},
    )
    with engine.begin() as conn:
        conn.execute(stmt)


def db_get_daily_stats(for_date: Optional[date] = None) -> Optional[dict]:
    d = for_date or date.today()
    with engine.connect() as conn:
        row = conn.execute(
            select(daily_stats).where(daily_stats.c.date == d)
        ).fetchone()
    return dict(row._mapping) if row else None


def log_signal(data: dict) -> int:
    row = {
        "timestamp": data.get("timestamp", datetime.utcnow()),
        "symbol": data.get("symbol"),
        "signal": data.get("signal"),
        "confidence": data.get("confidence"),
        "reasoning": data.get("reasoning"),
        "rule_score": data.get("rule_score"),
        "was_executed": bool(data.get("was_executed", False)),
        "filter_block_reason": data.get("filter_block_reason"),
    }
    with engine.begin() as conn:
        result = conn.execute(insert(signals_log).values(**row))
        return result.lastrowid


def save_signal(data: dict) -> int:
    return log_signal(data)


def save_portfolio_snapshot(data: dict) -> None:
    row = {
        "timestamp": data.get("timestamp", datetime.utcnow()),
        "equity": data.get("equity"),
        "cash": data.get("cash"),
        "buying_power": data.get("buying_power"),
        "open_positions": data.get("open_positions"),
    }
    with engine.begin() as conn:
        conn.execute(insert(portfolio_snapshots).values(**row))


def get_portfolio_history(limit: int = 200) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(portfolio_snapshots)
            .order_by(portfolio_snapshots.c.timestamp.asc())
            .limit(limit)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def get_recent_trades(limit: int = 50) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(trades)
            .where(trades.c.status != "open")
            .order_by(trades.c.entry_time.desc())
            .limit(limit)
        ).fetchall()
    results = []
    for r in rows:
        d = dict(r._mapping)
        d["fill_price"] = d.get("exit_price") or d.get("entry_price")
        d["timestamp"] = d.get("entry_time")
        results.append(d)
    return results


def get_recent_signals(limit: int = 100) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(signals_log)
            .order_by(signals_log.c.timestamp.desc())
            .limit(limit)
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def get_starting_equity(trade_date: date) -> Optional[float]:
    with engine.connect() as conn:
        row = conn.execute(
            select(daily_equity).where(daily_equity.c.trade_date == trade_date)
        ).fetchone()
    return float(row._mapping["starting_equity"]) if row else None


def set_starting_equity(trade_date: date, equity: float) -> None:
    stmt = sqlite_insert(daily_equity).values(
        trade_date=trade_date, starting_equity=equity
    )
    stmt = stmt.on_conflict_do_nothing(index_elements=["trade_date"])
    with engine.begin() as conn:
        conn.execute(stmt)


def db_compute_all_time_stats() -> dict:
    closed = db_get_all_closed_trades()
    pnls = [r["pnl"] for r in closed if r.get("pnl") is not None]
    pnl_pcts = [r["pnl_pct"] for r in closed if r.get("pnl_pct") is not None]

    if not pnls:
        return {
            "total_trades": 0,
            "trades_won": 0,
            "trades_lost": 0,
            "win_rate": 0.0,
            "avg_pnl": 0.0,
            "total_pnl": 0.0,
            "avg_pnl_pct": 0.0,
            "sharpe_ratio": 0.0,
            "max_drawdown_pct": 0.0,
        }

    total = len(pnls)
    won = sum(1 for p in pnls if p > 0)
    avg_pnl = sum(pnls) / total
    avg_pct = sum(pnl_pcts) / len(pnl_pcts) if pnl_pcts else 0.0

    sharpe = 0.0
    if len(pnl_pcts) > 1:
        variance = sum((p - avg_pct) ** 2 for p in pnl_pcts) / (len(pnl_pcts) - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        sharpe = avg_pct / std if std > 0 else 0.0

    max_dd = _compute_max_drawdown_from_snapshots()

    return {
        "total_trades": total,
        "trades_won": won,
        "trades_lost": total - won,
        "win_rate": round(won / total, 4),
        "avg_pnl": round(avg_pnl, 2),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl_pct": round(avg_pct * 100, 3),
        "sharpe_ratio": round(sharpe, 4),
        "max_drawdown_pct": round(max_dd, 4),
    }


def _compute_max_drawdown_from_snapshots() -> float:
    with engine.connect() as conn:
        rows = conn.execute(
            select(portfolio_snapshots.c.equity)
            .order_by(portfolio_snapshots.c.timestamp.asc())
        ).fetchall()

    equities = [float(r[0]) for r in rows if r[0] is not None]
    if len(equities) < 2:
        return 0.0

    peak = equities[0]
    max_dd = 0.0
    for eq in equities[1:]:
        if eq > peak:
            peak = eq
        elif peak > 0:
            dd = (peak - eq) / peak
            max_dd = max(max_dd, dd)
    return max_dd
