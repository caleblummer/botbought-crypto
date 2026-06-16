"""Crypto AI Day Trader — main entry point.

Usage:
    python main.py                  # paper mode (TRADING_MODE from .env)
    python main.py --mode paper     # force paper / dry-run
    python main.py --mode live      # live trading (requires confirmation)
"""
from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler

from config.settings import (
    BAR_TIMEFRAME,
    DAILY_CLOSE_UTC,
    MAX_DAILY_LOSS_PCT,
    MIN_TRADE_CAPITAL,
    SCAN_INTERVAL_MINUTES,
    TRADING_MODE,
    require_live_confirmation,
)
from config.watchlist import WATCHLIST
from data.fetcher import CryptoDataFetcher
from data.indicators import compute_indicators, get_signal_summary
from db.database import db_get_open_trades, init_db, log_signal, save_portfolio_snapshot
from execution.broker import BrokerError, CryptoBroker
from execution.portfolio import Portfolio, Position, PortfolioTracker
from logs.logger import (
    get_logger,
    log_daily_summary,
    log_exception,
    log_order,
    log_risk_halt,
    log_trade_close,
)
from risk.manager import RiskManager
from strategy.ai_signal import AISignalGenerator
from strategy.rules import RuleFilter, compute_signal_strength

logger = get_logger("main")

UTC = timezone.utc


class TradingBot:
    def __init__(self, dry_run: bool = True) -> None:
        self._dry_run = dry_run
        self._scan_lock = threading.Lock()
        self._loss_halt = False
        self._running = False

        self.fetcher: Optional[CryptoDataFetcher] = None
        self.broker: Optional[CryptoBroker] = None
        self.portfolio: Optional[PortfolioTracker] = None
        self.portfolio_db: Optional[Portfolio] = None
        self.ai_generator: Optional[AISignalGenerator] = None
        self.rule_filter: Optional[RuleFilter] = None
        self.risk: Optional[RiskManager] = None
        self.scheduler: Optional[BackgroundScheduler] = None

    def startup(self) -> None:
        _banner(
            f"Crypto AI Day Trader  |  "
            f"{'DRY RUN (paper)' if self._dry_run else '*** LIVE TRADING ***'}"
        )

        self.fetcher = CryptoDataFetcher()
        self.broker = CryptoBroker(dry_run=self._dry_run)
        self.portfolio = PortfolioTracker()
        self.portfolio_db = Portfolio()
        self.ai_generator = AISignalGenerator()
        self.rule_filter = RuleFilter()

        equity = 100_000.0
        try:
            acct = self.broker.get_account()
            equity = float(acct.get("equity", equity))
            free_usdt = self.portfolio_db.get_available_cash(acct)
            logger.info(
                f"Account:  equity=${equity:>12,.2f}  "
                f"free USDT=${free_usdt:>12,.2f}  "
                f"(BUY floor=${MIN_TRADE_CAPITAL:,.2f})"
            )
        except BrokerError as exc:
            logger.warning(f"Could not reach exchange: {exc}")

        self.risk = RiskManager(portfolio_value=equity)
        logger.info(
            f"Starting equity: ${self.risk._starting_equity:,.2f}  "
            f"(daily loss limit: {MAX_DAILY_LOSS_PCT:.1%})"
        )

        self._restore_positions_from_db()
        logger.info(f"Watching {len(WATCHLIST)} pairs: {', '.join(WATCHLIST)}")
        self._running = True

    def _restore_positions_from_db(self) -> None:
        open_trades = db_get_open_trades()
        for trade in open_trades:
            pos = Position(
                symbol=trade["symbol"],
                side=str(trade.get("side") or "buy"),
                qty=float(trade.get("qty") or 0),
                entry_price=float(trade.get("entry_price") or 0),
                stop_loss=float(trade.get("stop_loss") or 0),
                take_profit=float(trade.get("take_profit") or 0),
                order_id=str(trade.get("order_id") or ""),
                trade_id=trade["id"],
            )
            self.portfolio.add_position(pos)

    def scan_and_trade(self) -> None:
        if not self._scan_lock.acquire(blocking=False):
            logger.warning("scan_and_trade: previous cycle still running — skipping")
            return
        try:
            self._do_scan_and_trade()
        except Exception as exc:
            log_exception(exc, context="scan_and_trade")
        finally:
            self._scan_lock.release()

    def _do_scan_and_trade(self) -> None:
        try:
            acct = self.broker.get_account()
            current_equity = float(acct.get("equity", self.risk._portfolio_value))
            free_usdt = self.portfolio_db.get_available_cash(acct)
        except BrokerError as exc:
            logger.error(f"Could not fetch account: {exc}")
            acct = {}
            current_equity = self.risk._portfolio_value
            free_usdt = float("inf")

        if self._loss_halt:
            self._snapshot(acct, current_equity)
            return

        if self.risk.check_daily_loss_limit(self.risk._starting_equity, current_equity):
            self._loss_halt = True
            log_risk_halt(
                f"equity=${current_equity:,.2f}  "
                f"start=${self.risk._starting_equity:,.2f}  "
                f"limit={MAX_DAILY_LOSS_PCT:.1%}"
            )
            self._snapshot(acct, current_equity)
            return

        self.risk.update_portfolio_value(current_equity)
        buys_allowed = free_usdt >= MIN_TRADE_CAPITAL
        if not buys_allowed:
            logger.warning(
                f"Free USDT ${free_usdt:,.2f} < ${MIN_TRADE_CAPITAL:,.2f} — "
                "BUY signals skipped this cycle"
            )

        self._check_trailing_stops()

        prices: dict[str, float] = {}
        for pos in self.portfolio.all_positions():
            try:
                quote = self.fetcher.get_latest_quote(pos.symbol)
                prices[pos.symbol] = quote["mid_price"]
            except Exception as exc:
                logger.warning(f"Could not fetch quote for {pos.symbol}: {exc}")

        for pos, reason in self.portfolio.check_exits(prices):
            self._close_position(pos, prices.get(pos.symbol, pos.entry_price), reason)

        indicator_summaries: dict[str, dict] = {}
        price_summaries: dict[str, str] = {}
        dfs: dict[str, object] = {}
        eligible: list[str] = []

        for symbol in WATCHLIST:
            if self.portfolio.has_position(symbol):
                continue

            df_raw = self.fetcher.get_bars(symbol, timeframe=BAR_TIMEFRAME, limit=200)
            if df_raw is None or df_raw.empty:
                continue

            try:
                df = compute_indicators(df_raw)
                summary = get_signal_summary(df)
            except ValueError:
                continue

            try:
                quote = self.fetcher.get_latest_quote(symbol)
                summary["spread"] = quote.get("spread", 0.0)
                summary["mid_price"] = quote.get("mid_price", summary.get("price", 0.0))
                summary["quote_volume_24h"] = quote.get("quote_volume_24h", 0)
                summary["percentage_24h"] = quote.get("percentage_24h", 0)
            except Exception:
                summary.setdefault("spread", 0.0)
                summary.setdefault("mid_price", summary.get("price", 0.0))

            ok, _ = self.rule_filter.pre_filter(symbol, df, summary)
            if not ok:
                continue

            indicator_summaries[symbol] = summary
            price_summaries[symbol] = summary.get("summary_text", "")
            dfs[symbol] = df
            eligible.append(symbol)

        if not eligible:
            self._snapshot(acct, current_equity)
            return

        ranked = self.ai_generator.batch_analyze(
            eligible, indicator_summaries, price_summaries
        )

        port_state = {
            "open_positions": self.portfolio.open_count(),
            "held_symbols": {p.symbol for p in self.portfolio.all_positions()},
        }

        for ai_sig in ranked:
            self._process_signal(
                ai_sig, indicator_summaries, dfs, port_state,
                buys_allowed=buys_allowed,
            )

        self._snapshot(acct, current_equity)

    def _process_signal(
        self,
        ai: dict,
        indicator_summaries: dict[str, dict],
        dfs: dict,
        port_state: dict,
        buys_allowed: bool = True,
    ) -> None:
        symbol = ai["symbol"]
        ind = indicator_summaries.get(symbol, {})
        df = dfs.get(symbol)
        rule_score = compute_signal_strength(df) if df is not None else 0.0

        executed = False
        block_reason: Optional[str] = None

        if ai["signal"] == "BUY" and not buys_allowed:
            block_reason = f"insufficient USDT (< ${MIN_TRADE_CAPITAL:,.2f})"
        elif ai["signal"] == "HOLD":
            block_reason = "AI signal: HOLD"
        else:
            ok, post_reason = self.rule_filter.post_filter(ai, port_state)
            if not ok:
                block_reason = post_reason
            else:
                if ai.get("suggested_entry") is not None:
                    entry_price = float(ai["suggested_entry"])
                else:
                    try:
                        lq = self.fetcher.get_latest_quote(symbol)
                        entry_price = lq["mid_price"]
                    except Exception:
                        entry_price = float(ind.get("mid_price") or ind.get("price") or 0)

                if entry_price <= 0:
                    block_reason = "could not determine a valid entry price"
                else:
                    side = "buy" if ai["signal"] == "BUY" else "sell"
                    atr = ind.get("atr_14")
                    spec = self.risk.size_position(
                        symbol,
                        entry_price,
                        side=side,
                        confidence=ai["confidence"],
                        atr=float(atr) if atr is not None else None,
                        stop_loss_price=ai.get("suggested_stop_loss"),
                        take_profit_price=ai.get("suggested_take_profit"),
                    )

                    if spec is None:
                        block_reason = "position sizing returned 0 qty"
                    else:
                        try:
                            order = self.broker.submit_bracket_order(
                                symbol=spec.symbol,
                                qty=spec.qty,
                                side=spec.side,
                                entry_price=spec.entry_price,
                                stop_loss=spec.stop_loss,
                                take_profit=spec.take_profit,
                            )
                            executed = True
                            trade_id = self.portfolio_db.record_trade_open({
                                "symbol": symbol,
                                "side": side,
                                "qty": spec.qty,
                                "entry_price": spec.entry_price,
                                "stop_loss": spec.stop_loss,
                                "take_profit": spec.take_profit,
                                "order_id": order["order_id"],
                                "ai_signal_confidence": ai["confidence"],
                                "ai_reasoning": ai["reasoning"],
                            })
                            pos = Position(
                                symbol=symbol,
                                side=side,
                                qty=spec.qty,
                                entry_price=spec.entry_price,
                                stop_loss=spec.stop_loss,
                                take_profit=spec.take_profit,
                                order_id=order["order_id"],
                                trade_id=trade_id,
                            )
                            self.portfolio.add_position(pos)
                            port_state["open_positions"] += 1
                            port_state["held_symbols"].add(symbol)
                            log_order(
                                symbol=symbol,
                                side=side,
                                qty=spec.qty,
                                price=spec.entry_price,
                                order_id=order["order_id"],
                                status=order.get("status", "submitted"),
                            )
                        except BrokerError as exc:
                            block_reason = f"broker: {exc}"

        log_signal({
            "symbol": symbol,
            "signal": ai["signal"],
            "confidence": ai["confidence"],
            "rule_score": rule_score,
            "reasoning": ai["reasoning"],
            "was_executed": executed,
            "filter_block_reason": block_reason,
        })

    def _check_trailing_stops(self) -> None:
        for pos in list(self.portfolio.all_positions()):
            try:
                quote = self.fetcher.get_latest_quote(pos.symbol)
                price = quote["mid_price"]
                pos.update_highest_price(price)
                if pos.highest_price is None:
                    continue

                df_raw = self.fetcher.get_bars(pos.symbol, timeframe=BAR_TIMEFRAME, limit=50)
                if df_raw is None or df_raw.empty or len(df_raw) < 30:
                    continue
                df = compute_indicators(df_raw)
                if df.empty:
                    continue
                atr = float(df.iloc[-1].get("atr_14") or 0)
                if atr <= 0:
                    continue

                new_stop = self.risk.get_trailing_stop_update(
                    price, pos.highest_price, atr
                )
                if pos.side == "buy" and new_stop > pos.stop_loss:
                    pos.stop_loss = new_stop
                elif pos.side == "sell" and 0 < new_stop < pos.stop_loss:
                    pos.stop_loss = new_stop
            except Exception as exc:
                logger.warning(f"Trailing stop check failed for {pos.symbol}: {exc}")

    def _close_position(self, pos: Position, exit_price: float, reason: str) -> None:
        try:
            self.broker.close_position(pos.symbol)
        except BrokerError as exc:
            logger.error(f"close_position failed for {pos.symbol}: {exc}")

        self.portfolio.remove_position(pos.symbol)
        if pos.trade_id is not None:
            result = self.portfolio_db.record_trade_close(
                trade_id=pos.trade_id,
                exit_price=exit_price,
                exit_time=datetime.utcnow(),
                status=reason,
            )
            hold_secs = (datetime.utcnow() - pos.opened_at).total_seconds()
            log_trade_close(
                symbol=pos.symbol,
                entry=pos.entry_price,
                exit=exit_price,
                pnl=result.get("pnl", 0.0),
                pnl_pct=result.get("pnl_pct", 0.0),
                hold_duration=hold_secs,
            )

    def end_of_day(self) -> None:
        if not DAILY_CLOSE_UTC:
            return
        _banner(f"Daily close sequence starting (UTC {DAILY_CLOSE_UTC})")
        with self._scan_lock:
            self._do_end_of_day()

    def _do_end_of_day(self) -> None:
        try:
            self.broker.close_all_positions_eod()
        except BrokerError as exc:
            logger.error(f"Daily close: position close failed: {exc}")

        for pos in list(self.portfolio.all_positions()):
            try:
                quote = self.fetcher.get_latest_quote(pos.symbol)
                exit_price = quote["mid_price"]
            except Exception:
                exit_price = pos.entry_price
            self.portfolio.remove_position(pos.symbol)
            if pos.trade_id is not None:
                self.portfolio_db.record_trade_close(
                    trade_id=pos.trade_id,
                    exit_price=exit_price,
                    exit_time=datetime.utcnow(),
                    status="daily_close",
                )

        try:
            self.broker.cancel_all_orders()
        except BrokerError as exc:
            logger.error(f"Daily close: cancel orders failed: {exc}")

        try:
            acct = self.broker.get_account()
            ending_equity = float(acct.get("equity", self.risk._starting_equity))
        except BrokerError:
            ending_equity = self.risk._starting_equity

        starting_equity = self.risk._starting_equity or ending_equity
        self.portfolio_db.update_daily_stats(ending_equity, starting_equity)
        stats = self.portfolio_db.get_today_stats()
        log_daily_summary({
            **stats,
            "starting_equity": starting_equity,
            "ending_equity": ending_equity,
        })
        self.risk.reset_day(ending_equity)
        self._loss_halt = False

    def shutdown(self) -> None:
        if not self._running:
            return
        self._running = False

        acquired = self._scan_lock.acquire(timeout=30)
        try:
            try:
                self.broker.close_all_positions_eod()
            except BrokerError:
                pass
            for pos in list(self.portfolio.all_positions()):
                try:
                    quote = self.fetcher.get_latest_quote(pos.symbol)
                    exit_price = quote["mid_price"]
                except Exception:
                    exit_price = pos.entry_price
                self.portfolio.remove_position(pos.symbol)
                if pos.trade_id is not None:
                    self.portfolio_db.record_trade_close(
                        trade_id=pos.trade_id,
                        exit_price=exit_price,
                        exit_time=datetime.utcnow(),
                        status="cancelled_shutdown",
                    )
            try:
                self.broker.cancel_all_orders()
            except BrokerError:
                pass
        finally:
            if acquired:
                self._scan_lock.release()

        if self.scheduler and self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def _snapshot(self, acct: dict, equity: float) -> None:
        try:
            save_portfolio_snapshot({
                "equity": equity,
                "cash": float(acct.get("cash", 0)),
                "buying_power": float(acct.get("buying_power", 0)),
                "open_positions": self.portfolio.open_count(),
            })
        except Exception as exc:
            logger.warning(f"Could not save portfolio snapshot: {exc}")


def _banner(text: str) -> None:
    line = "=" * max(len(text) + 4, 56)
    logger.info(line)
    logger.info(f"  {text}")
    logger.info(line)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crypto AI day trading bot")
    parser.add_argument(
        "--mode",
        choices=["paper", "live"],
        default=None,
        help="Trading mode override",
    )
    return parser.parse_args()


def _resolve_dry_run(mode_arg: Optional[str]) -> bool:
    if mode_arg == "paper":
        return True
    if mode_arg == "live" and TRADING_MODE != "live":
        print("\nERROR: --mode live requires TRADING_MODE=live in .env\n")
        sys.exit(1)
    if TRADING_MODE == "live":
        require_live_confirmation()
        return False
    return True


def _parse_daily_close_hour_minute() -> tuple[int, int] | None:
    if not DAILY_CLOSE_UTC:
        return None
    try:
        parts = DAILY_CLOSE_UTC.split(":")
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        logger.warning(f"Invalid DAILY_CLOSE_UTC={DAILY_CLOSE_UTC!r}; ignoring")
        return None


def main() -> None:
    args = _parse_args()
    dry_run = _resolve_dry_run(args.mode)
    init_db()

    bot = TradingBot(dry_run=dry_run)
    bot.startup()

    def _handle_signal(signum: int, frame) -> None:
        logger.info(f"Received {signal.Signals(signum).name} — shutting down")
        bot.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    scheduler = BackgroundScheduler(timezone=UTC)
    bot.scheduler = scheduler

    scheduler.add_job(
        bot.scan_and_trade,
        trigger="interval",
        minutes=SCAN_INTERVAL_MINUTES,
        id="scan_and_trade",
        max_instances=1,
        coalesce=True,
    )

    daily_close = _parse_daily_close_hour_minute()
    if daily_close:
        h, m = daily_close
        scheduler.add_job(
            bot.end_of_day,
            trigger="cron",
            hour=h,
            minute=m,
            id="end_of_day",
            timezone=UTC,
            max_instances=1,
        )

    scheduler.start()

    now_utc = datetime.now(UTC)
    logger.info(
        f"Scheduler running  |  UTC {now_utc.strftime('%H:%M')}  |  "
        f"scan every {SCAN_INTERVAL_MINUTES} min (24/7)  |  "
        f"{'DRY RUN' if dry_run else 'LIVE'}"
    )
    if daily_close:
        logger.info(f"Daily close at {DAILY_CLOSE_UTC} UTC")
    else:
        logger.info("Daily close disabled — positions may be held overnight")
    logger.info("Press Ctrl+C to stop cleanly")

    try:
        while bot._running:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
