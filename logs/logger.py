"""Logging configuration and structured trade-event helpers."""
from __future__ import annotations

import json
import os
import smtplib
import sys
import threading
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from typing import Any

from dotenv import load_dotenv
from loguru import logger as _logger

load_dotenv()

_ALERT_EMAIL: str = os.getenv("ALERT_EMAIL", "")
_SMTP_USER: str = os.getenv("ALERT_SMTP_USER", "")
_SMTP_PASSWORD: str = os.getenv("ALERT_SMTP_PASSWORD", "")
_SMTP_HOST: str = os.getenv("ALERT_SMTP_HOST", "smtp.gmail.com")
_SMTP_PORT: int = int(os.getenv("ALERT_SMTP_PORT", "465"))
_LARGE_LOSS_THRESHOLD_PCT: float = -3.0

_LOG_DIR = "logs"
_configured = False


def _configure() -> None:
    global _configured
    if _configured:
        return

    os.makedirs(_LOG_DIR, exist_ok=True)
    _logger.remove()
    _logger.configure(
        patcher=lambda r: r["extra"].setdefault("name", r["name"])
    )

    _logger.add(
        sys.stderr,
        format=(
            "<green>{time:HH:mm:ss}</green> | "
            "<level>{level:<8}</level> | "
            "<level>{message}</level>"
        ),
        level="INFO",
        colorize=True,
        backtrace=False,
        diagnose=False,
    )

    _logger.add(
        os.path.join(_LOG_DIR, "trader_{time:YYYY-MM-DD}.log"),
        rotation="00:00",
        retention="30 days",
        level="DEBUG",
        format=(
            "{time:YYYY-MM-DD HH:mm:ss.SSS} | "
            "{level:<8} | "
            "{extra[name]:<24} | "
            "{message}"
        ),
        encoding="utf-8",
        backtrace=True,
        diagnose=False,
    )

    _configured = True


def get_logger(name: str):
    _configure()
    return _logger.bind(name=name)


_L = get_logger("logger")


def _write_trade_event(event_type: str, data: dict[str, Any]) -> None:
    _configure()
    today = datetime.utcnow().date().isoformat()
    path = os.path.join(_LOG_DIR, f"trades_{today}.jsonl")
    record: dict[str, Any] = {
        "ts": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "event": event_type,
    }
    record.update(data)
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:
        _L.warning(f"trade JSONL write failed ({path}): {exc}")


def _send_alert(subject: str, body: str) -> None:
    if not (_ALERT_EMAIL and _SMTP_USER and _SMTP_PASSWORD):
        return
    try:
        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = f"[Crypto AI Trader] {subject}"
        msg["From"] = _SMTP_USER
        msg["To"] = _ALERT_EMAIL
        with smtplib.SMTP_SSL(_SMTP_HOST, _SMTP_PORT, timeout=15) as server:
            server.login(_SMTP_USER, _SMTP_PASSWORD)
            server.sendmail(_SMTP_USER, [_ALERT_EMAIL], msg.as_string())
    except Exception as exc:
        _L.warning(f"Alert email failed ({subject!r}): {exc}")


def _send_alert_async(subject: str, body: str) -> None:
    if not _ALERT_EMAIL:
        return
    threading.Thread(
        target=_send_alert,
        args=(subject, body),
        daemon=True,
        name="alert-email",
    ).start()


def log_signal(
    symbol: str,
    signal: str,
    confidence: float,
    reasoning: str,
    executed: bool,
) -> None:
    _configure()
    _logger.bind(name="signal").debug(
        f"SIGNAL  {signal:<4}  {symbol:<12}  "
        f"conf={confidence:.0%}  executed={executed}  ·  {reasoning}"
    )


def log_order(
    symbol: str,
    side: str,
    qty: float,
    price: float,
    order_id: str,
    status: str,
) -> None:
    _configure()
    _logger.bind(name="order").info(
        f"ORDER  {side.upper():<4}  {qty:>12.6f} × {symbol:<12}  "
        f"@ ${price:.6f}  status={status}  id={order_id}"
    )
    _write_trade_event("order", {
        "symbol": symbol,
        "side": side.lower(),
        "qty": qty,
        "price": round(price, 8),
        "order_id": order_id,
        "status": status,
    })


def log_trade_close(
    symbol: str,
    entry: float,
    exit: float,
    pnl: float,
    pnl_pct: float,
    hold_duration: timedelta | float,
) -> None:
    _configure()
    hold_secs = (
        hold_duration.total_seconds()
        if isinstance(hold_duration, timedelta)
        else float(hold_duration)
    )
    hold_str = _fmt_duration(hold_secs)
    sign = "+" if pnl >= 0 else ""

    _logger.bind(name="trade").info(
        f"CLOSE  {symbol:<12}  "
        f"entry=${entry:.6f}  exit=${exit:.6f}  "
        f"PnL={sign}${pnl:.2f} ({sign}{pnl_pct:.2f}%)  "
        f"held={hold_str}"
    )
    _write_trade_event("trade_close", {
        "symbol": symbol,
        "entry": round(entry, 8),
        "exit": round(exit, 8),
        "pnl": round(pnl, 4),
        "pnl_pct": round(pnl_pct, 4),
        "hold_seconds": round(hold_secs, 1),
    })

    if pnl_pct < _LARGE_LOSS_THRESHOLD_PCT:
        _send_alert_async(
            f"Large loss on {symbol}: {sign}{pnl_pct:.1f}%",
            (
                f"A trade closed with a significant loss.\n\n"
                f"Pair      : {symbol}\n"
                f"Entry     : ${entry:.6f}\n"
                f"Exit      : ${exit:.6f}\n"
                f"P&L       : {sign}${pnl:.2f}  ({sign}{pnl_pct:.2f}%)\n"
                f"Duration  : {hold_str}\n"
            ),
        )


def log_daily_summary(stats: dict[str, Any]) -> None:
    _configure()
    trades = int(stats.get("trades_taken", 0))
    won = int(stats.get("trades_won", 0))
    lost = int(stats.get("trades_lost", 0))
    win_rate = float(stats.get("win_rate", 0))
    total_pnl = float(stats.get("total_pnl", 0))
    avg_pnl = float(stats.get("avg_pnl", 0))

    _logger.bind(name="summary").info(
        f"DAILY SUMMARY  "
        f"trades={trades}  won={won}  lost={lost}  "
        f"win_rate={win_rate:.1%}  "
        f"total_pnl=${total_pnl:+,.2f}  "
        f"avg_pnl=${avg_pnl:+.2f}"
    )
    _write_trade_event("daily_summary", {
        k: (round(float(v), 6) if isinstance(v, float) else v)
        for k, v in stats.items()
    })


def log_risk_halt(reason: str) -> None:
    _configure()
    _logger.bind(name="risk").warning(f"RISK HALT — {reason}")
    _write_trade_event("risk_halt", {"reason": reason})
    _send_alert_async(
        "Daily loss limit breached — trading halted",
        (
            f"The Crypto AI Trader has halted new trades for today.\n\n"
            f"Reason : {reason}\n\n"
            f"No new orders will be placed until the next daily reset.\n"
        ),
    )


def log_exception(exc: Exception, context: str = "") -> None:
    _configure()
    prefix = f"{context} — " if context else ""
    _logger.bind(name="error").exception(
        f"{prefix}{type(exc).__name__}: {exc}"
    )
    _send_alert_async(
        f"Unhandled exception: {type(exc).__name__}",
        (
            f"An unexpected error occurred in the trading bot.\n\n"
            f"Context : {context or '(none)'}\n"
            f"Error   : {type(exc).__name__}: {exc}\n"
        ),
    )


def _fmt_duration(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"
    h, rem = divmod(int(seconds), 3600)
    m = rem // 60
    return f"{h}h{m:02d}m"
