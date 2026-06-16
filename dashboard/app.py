"""Crypto AI Day Trader — Streamlit dashboard.

Run separately from the bot:
    streamlit run dashboard/app.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

load_dotenv(os.path.join(_ROOT, ".env"))

from config.settings import (
    EXCHANGE_API_KEY,
    EXCHANGE_ID,
    EXCHANGE_SECRET,
    EXCHANGE_SANDBOX,
    MIN_TRADE_CAPITAL,
    TRADING_MODE,
)
from db.database import (
    db_get_daily_stats,
    db_get_open_trades,
    db_get_today_trades,
    get_recent_signals,
    init_db,
)

init_db()

st.set_page_config(
    page_title="Crypto AI Day Trader",
    page_icon="₿",
    layout="wide",
)

_REFRESH_SECS = 30


def _get_live_prices(symbols: tuple[str, ...]) -> dict[str, float]:
    if not symbols:
        return {}
    try:
        import ccxt

        if not hasattr(ccxt, EXCHANGE_ID):
            return {}
        exchange_cls = getattr(ccxt, EXCHANGE_ID)
        config = {"enableRateLimit": True}
        if EXCHANGE_API_KEY:
            config["apiKey"] = EXCHANGE_API_KEY
            config["secret"] = EXCHANGE_SECRET
        ex = exchange_cls(config)
        if EXCHANGE_SANDBOX and hasattr(ex, "set_sandbox_mode"):
            ex.set_sandbox_mode(True)
        prices = {}
        for sym in symbols:
            ticker = ex.fetch_ticker(sym)
            prices[sym] = float(ticker.get("last") or ticker.get("close") or 0)
        return prices
    except Exception:
        return {}


def _get_account() -> dict:
    try:
        import ccxt

        if not (EXCHANGE_API_KEY and hasattr(ccxt, EXCHANGE_ID)):
            return {}
        exchange_cls = getattr(ccxt, EXCHANGE_ID)
        ex = exchange_cls({
            "apiKey": EXCHANGE_API_KEY,
            "secret": EXCHANGE_SECRET,
            "enableRateLimit": True,
        })
        if EXCHANGE_SANDBOX and hasattr(ex, "set_sandbox_mode"):
            ex.set_sandbox_mode(True)
        balance = ex.fetch_balance()
        free = balance.get("free", {})
        total = balance.get("total", {})
        usdt_free = float(free.get("USDT") or free.get("USD") or 0)
        usdt_total = float(total.get("USDT") or total.get("USD") or usdt_free)
        return {"equity": usdt_total, "free_usdt": usdt_free, "cash": usdt_free}
    except Exception:
        return {}


def _force_close_all() -> tuple[bool, str]:
    try:
        from execution.broker import CryptoBroker

        broker = CryptoBroker(dry_run=False)
        broker.cancel_all_orders()
        positions = broker.get_positions()
        if not positions:
            return True, "No open positions to close."
        count = 0
        for pos in positions:
            try:
                broker.close_position(pos["symbol"])
                count += 1
            except Exception:
                pass
        return True, f"Closed {count} position(s). All orders cancelled."
    except Exception as exc:
        return False, str(exc)


@st.cache_data(ttl=30)
def _load_today_trades() -> list[dict]:
    return db_get_today_trades()


@st.cache_data(ttl=30)
def _load_open_trades() -> list[dict]:
    return db_get_open_trades()


@st.cache_data(ttl=30)
def _load_signals() -> list[dict]:
    return get_recent_signals(limit=20)


@st.cache_data(ttl=60)
def _load_equity_curve() -> list[dict]:
    from sqlalchemy import select
    from db.database import engine, daily_stats

    cutoff = date.today() - timedelta(days=90)
    with engine.connect() as conn:
        rows = conn.execute(
            select(daily_stats.c.date, daily_stats.c.ending_equity)
            .where(daily_stats.c.date >= cutoff)
            .where(daily_stats.c.ending_equity.isnot(None))
            .order_by(daily_stats.c.date.asc())
        ).fetchall()
    return [{"date": r[0], "equity": r[1]} for r in rows]


def _today_kpis(today_trades: list[dict]) -> dict:
    closed = [t for t in today_trades if t.get("status") not in ("open",)]
    pnls = [float(t["pnl"]) for t in closed if t.get("pnl") is not None]
    total = len(closed)
    won = sum(1 for p in pnls if p > 0)
    starting = 0.0
    stats = db_get_daily_stats(date.today())
    if stats:
        starting = float(stats.get("starting_equity") or 0)
    total_pnl = sum(pnls)
    return {
        "total_pnl": total_pnl,
        "pnl_pct": (total_pnl / starting * 100) if starting else 0.0,
        "win_rate": (won / total * 100) if total else 0.0,
        "trades_taken": total,
        "open_positions": sum(1 for t in today_trades if t.get("status") == "open"),
    }


def _sidebar() -> None:
    st.sidebar.title("Crypto AI Trader")
    mode = "LIVE" if TRADING_MODE == "live" else "PAPER"
    st.sidebar.markdown(f"**Mode:** {mode}")
    st.sidebar.markdown("**Market:** 24/7")
    st.sidebar.caption(f"Today: {date.today().strftime('%A, %b %d %Y')}")
    st.sidebar.divider()

    if st.sidebar.button("Refresh now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()

    if "last_refresh" not in st.session_state:
        st.session_state.last_refresh = time.time()
    elapsed = time.time() - st.session_state.last_refresh
    if elapsed >= _REFRESH_SECS:
        st.session_state.last_refresh = time.time()
        st.cache_data.clear()
        st.rerun()
    st.sidebar.caption(f"Auto-refresh in {max(0, int(_REFRESH_SECS - elapsed))}s")

    st.sidebar.divider()
    if st.sidebar.button("Force Close All", type="primary", use_container_width=True):
        st.session_state["confirm_close"] = True
    if st.session_state.get("confirm_close"):
        st.sidebar.warning("Close ALL positions and cancel orders?")
        if st.sidebar.button("Yes, close all"):
            ok, msg = _force_close_all()
            st.sidebar.success(msg) if ok else st.sidebar.error(msg)
            st.session_state["confirm_close"] = False
            st.cache_data.clear()


def main() -> None:
    _sidebar()
    st.title("Crypto AI Day Trader")

    today_trades = _load_today_trades()
    open_trades = _load_open_trades()
    signals = _load_signals()
    curve_data = _load_equity_curve()
    kpis = _today_kpis(today_trades)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Today's P&L", f"${kpis['total_pnl']:+,.2f}")
    c2.metric("P&L %", f"{kpis['pnl_pct']:+.2f}%")
    c3.metric("Win Rate", f"{kpis['win_rate']:.1f}%")
    c4.metric("Trades Closed", kpis["trades_taken"])
    c5.metric("Open Positions", kpis["open_positions"])

    acct = _get_account()
    if acct:
        free = float(acct.get("free_usdt", 0))
        d1, d2 = st.columns(2)
        d1.metric(
            "Free USDT",
            f"${free:,.2f}",
            delta="buys allowed" if free >= MIN_TRADE_CAPITAL else "buys blocked",
        )
        d2.metric("Live Equity", f"${acct.get('equity', 0):,.2f}")

    st.divider()
    st.subheader(f"Open Positions ({len(open_trades)})")
    if open_trades:
        symbols = tuple(t["symbol"] for t in open_trades)
        live_prices = _get_live_prices(symbols)
        rows = []
        for t in open_trades:
            sym = t["symbol"]
            entry = float(t.get("entry_price") or 0)
            current = live_prices.get(sym, entry)
            qty = float(t.get("qty") or 0)
            side = str(t.get("side", "buy")).lower()
            unr_pnl = (current - entry) * qty * (1 if side == "buy" else -1)
            rows.append({
                "Pair": sym,
                "Side": side.upper(),
                "Qty": f"{qty:.6f}",
                "Entry": f"${entry:.6f}",
                "Current": f"${current:.6f}",
                "Unr. P&L": f"${unr_pnl:+.2f}",
            })
        st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    else:
        st.info("No open positions.")

    st.divider()
    st.subheader("Equity Curve")
    if curve_data:
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=[r["date"] for r in curve_data],
            y=[r["equity"] for r in curve_data],
            mode="lines+markers",
        ))
        fig.update_layout(height=300, margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)
    else:
        st.info("No daily equity data yet.")

    st.divider()
    st.subheader("Recent Signals")
    if signals:
        st.dataframe(pd.DataFrame([
            {
                "Time": s.get("timestamp"),
                "Pair": s.get("symbol"),
                "Signal": s.get("signal"),
                "Conf.": f"{float(s.get('confidence') or 0):.0%}",
                "Executed": "Yes" if s.get("was_executed") else "No",
                "Reason": str(s.get("reasoning") or "")[:80],
            }
            for s in signals
        ]), use_container_width=True, hide_index=True)
    else:
        st.info("No signals recorded yet.")


main()
