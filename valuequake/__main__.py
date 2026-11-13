import argparse
import logging
from datetime import date, datetime, timedelta

from apscheduler.schedulers.blocking import BlockingScheduler

from valuequake.alpaca import NY
from sqlalchemy import text

from valuequake.base_rates import refresh_base_rates
from valuequake.backfill import backfill, backfill_articles, backfill_macro
from valuequake.clients import make_client
from valuequake.config import load_config
from valuequake.daily_job import catch_up, refresh_calendar, run_daily_job
from valuequake.db import apply_schema, make_engine
from valuequake.gaps import bar_gaps


def main() -> None:
    parser = argparse.ArgumentParser(prog="valuequake")
    sub = parser.add_subparsers(dest="cmd")
    daily = sub.add_parser("daily", help="run the daily job once for a session date")
    daily.add_argument("session", type=date.fromisoformat)
    bf = sub.add_parser("backfill", help="load 1-min bars, dividends, Articles, FRED series and Scheduled Releases; resumable")
    bf.add_argument("--years", type=int, default=5)
    gaps = sub.add_parser("gaps", help="list sessions per ticker with missing or partial RTH bars")
    gaps.add_argument("--min-coverage", type=float, default=0.95)
    sub.add_parser("base-rates", help="recompute Base Rates now, up to the latest closed session")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO, and FRED's api_key travels in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    engine = make_engine()
    apply_schema(engine)
    if args.cmd == "gaps":
        for ticker, session, rth_bars, expected in bar_gaps(engine, datetime.now(NY), args.min_coverage):
            print(f"{ticker:5} {session}  {rth_bars:4}/{expected} RTH bars")
        return

    config = load_config()
    if args.cmd == "base-rates":
        with engine.begin() as conn:
            latest = conn.execute(text("SELECT max(date) FROM trading_sessions WHERE close_at <= now()")).scalar()
            print(refresh_base_rates(conn, config, latest))
        return

    client = make_client()
    if args.cmd == "backfill":
        now = datetime.now(NY)
        first_month = date(now.year - args.years, now.month, 1)
        backfill(engine, client, config.tickers, first_month, now)
        backfill_articles(engine, client, first_month, now)
        backfill_macro(engine, client, config, first_month, now)
        return
    if args.cmd == "daily":
        today = datetime.now(NY).date()
        refresh_calendar(engine, client, args.session - timedelta(days=10), today + timedelta(days=30), today)
        raise SystemExit(0 if run_daily_job(engine, client, config, args.session) else 1)

    # Hourly ticks are cheap no-ops when nothing is due, and catch up soon after the laptop wakes.
    scheduler = BlockingScheduler(timezone=NY)
    scheduler.add_job(
        lambda: catch_up(engine, client, config, datetime.now(NY)),
        "interval", hours=1, next_run_time=datetime.now(NY), coalesce=True, misfire_grace_time=None,
    )
    logging.info("scheduler started")
    scheduler.start()


if __name__ == "__main__":
    main()
