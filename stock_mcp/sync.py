"""
Stock data sync script — sync ALL A-share historical data to local database.

Designed to be run via cron or manually. Fetches from Sina Finance (K-line)
and akshare (fundamentals) during off-peak hours to avoid API instability.

Usage:
    # Sync K-line for all stocks (one-time or catch-up)
    python /opt/hermes-agent/stock_mcp/sync.py kline --all --delay 0.3

    # Sync K-line for specific stocks
    python /opt/hermes-agent/stock_mcp/sync.py kline --codes 000001,600519

    # Sync K-line for stocks missing data (resume/backfill)
    python /opt/hermes-agent/stock_mcp/sync.py kline --missing

    # Sync financial data
    python /opt/hermes-agent/stock_mcp/sync.py financial --all

    # Sync company profiles
    python /opt/hermes-agent/stock_mcp/sync.py company --all

    # Scheduled daily sync (run after market close via cron)
    python /opt/hermes-agent/stock_mcp/sync.py daily

    # Show sync status
    python /opt/hermes-agent/stock_mcp/sync.py status

Recommended cron (after market close, off-peak):
    30 16 * * 1-5 /opt/hermes-agent/.venv/bin/python /opt/hermes-agent/stock_mcp/sync.py daily
    0 17 * * 0   /opt/hermes-agent/.venv/bin/python /opt/hermes-agent/stock_mcp/sync.py financial --missing
"""

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, time as dt_time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("stock_sync")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_DB_PATH: Path | None = None
_db_conn: sqlite3.Connection | None = None

_SINA_SCALE = {"daily": 240, "weekly": 1200, "monthly": 7200}

_HTTP_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; StockSync/2.0)",
}


def _get_db_path() -> Path:
    global _DB_PATH
    if _DB_PATH is not None:
        return _DB_PATH
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    _DB_PATH = hermes_home / "stock_data.db"
    return _DB_PATH


def _get_db_conn() -> sqlite3.Connection:
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(db_path, check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    return _db_conn


def _init_db_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS stock_basic (
            code TEXT PRIMARY KEY,
            name TEXT,
            industry TEXT,
            market TEXT,
            list_date TEXT,
            is_hs TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS stock_financial (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            report_date TEXT,
            roe REAL,
            net_profit REAL,
            revenue REAL,
            total_assets REAL,
            total_liabilities REAL,
            debt_ratio REAL,
            updated_at TEXT,
            UNIQUE(code, report_date)
        );

        CREATE TABLE IF NOT EXISTS stock_company (
            code TEXT PRIMARY KEY,
            name TEXT,
            chairman TEXT,
            manager TEXT,
            secretary TEXT,
            reg_capital REAL,
            setup_date TEXT,
            province TEXT,
            city TEXT,
            introduction TEXT,
            website TEXT,
            email TEXT,
            office TEXT,
            updated_at TEXT
        );

        CREATE TABLE IF NOT EXISTS stock_kline (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            code TEXT,
            period TEXT,
            date TEXT,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume REAL,
            updated_at TEXT,
            UNIQUE(code, period, date)
        );

        CREATE TABLE IF NOT EXISTS sync_progress (
            code TEXT PRIMARY KEY,
            kline_synced INTEGER DEFAULT 0,
            kline_date TEXT,
            financial_synced INTEGER DEFAULT 0,
            company_synced INTEGER DEFAULT 0,
            updated_at TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_financial_code ON stock_financial(code);
        CREATE INDEX IF NOT EXISTS idx_financial_date ON stock_financial(report_date);
        CREATE INDEX IF NOT EXISTS idx_kline_code_period ON stock_kline(code, period);
        CREATE INDEX IF NOT EXISTS idx_kline_date ON stock_kline(date);
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_sina_code(code: str) -> str:
    c = code.strip().lower()
    c = c.removesuffix(".sh").removesuffix(".sz")
    if c.startswith(("sz", "sh")):
        return c
    c = c.zfill(6)
    if c.startswith(("6", "9")):
        return f"sh{c}"
    return f"sz{c}"


def _http_get(url: str, timeout: int = 10) -> str:
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # Sina returns HTTP 456 when rate-limited. Let caller handle.
        if resp.status == 456:
            raise urllib.error.HTTPError(
                url, 456, "Rate limited", resp.headers, None
            )
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "gbk"
        return raw.decode(charset, errors="replace")


# ---------------------------------------------------------------------------
# K-line sync (Sina Finance)
# ---------------------------------------------------------------------------


def _fetch_sina_kline(code: str, period: str, count: int = 500) -> list[dict]:
    """Fetch K-line data from Sina Finance. Returns list of bar dicts."""
    sina_code = _resolve_sina_code(code)
    scale = _SINA_SCALE.get(period, 240)
    count = min(count, 1000)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={sina_code}&scale={scale}"
        f"&ma=no&datalen={count}"
    )
    text = _http_get(url)
    data = json.loads(text)
    klines = []
    for d in data:
        klines.append({
            "date": d["day"],
            "open": float(d["open"]),
            "close": float(d["close"]),
            "high": float(d["high"]),
            "low": float(d["low"]),
            "volume": float(d["volume"]),
        })
    return klines


def _save_klines(conn: sqlite3.Connection, code: str, period: str, klines: list[dict], now: str) -> int:
    """Insert K-line bars into database. Returns number inserted."""
    cursor = conn.cursor()
    count = 0
    for bar in klines:
        cursor.execute("""
            INSERT OR REPLACE INTO stock_kline
            (code, period, date, open, high, low, close, volume, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            code, period,
            bar["date"], bar["open"], bar["high"], bar["low"],
            bar["close"], bar["volume"], now,
        ))
        count += 1
    return count


def sync_klines(
    codes: list[str] | None = None,
    missing_only: bool = False,
    period: str = "daily",
    count: int = 500,
    delay: float = 0.5,
    batch_size: int = 50,
    batch_pause: float = 5.0,
) -> dict:
    """Sync K-line data for stocks.

    Args:
        codes: Specific codes to sync. If None and missing_only=False, syncs ALL.
        missing_only: Only sync stocks that have no kline data.
        period: 'daily', 'weekly', or 'monthly'.
        count: Bars to fetch per stock (max 1000).
        delay: Seconds between individual stock requests.
        batch_size: Commit after this many stocks.
        batch_pause: Seconds to pause between batches.
    """
    now = datetime.now().isoformat()
    conn = _get_db_conn()

    # Determine which codes to sync
    cursor = conn.cursor()
    if codes:
        target_codes = [c.strip().zfill(6) for c in codes]
    elif missing_only:
        cursor.execute("""
            SELECT b.code FROM stock_basic b
            WHERE NOT EXISTS (SELECT 1 FROM stock_kline k WHERE k.code = b.code AND k.period = ?)
            ORDER BY b.code
        """, (period,))
        target_codes = [r[0] for r in cursor.fetchall()]
    else:
        cursor.execute("SELECT code FROM stock_basic ORDER BY code")
        target_codes = [r[0] for r in cursor.fetchall()]

    total = len(target_codes)
    synced_stocks = 0
    total_bars = 0
    failed = 0
    skipped = 0

    logger.info("K-line sync: %d stocks (period=%s, count=%d)", total, period, count)

    for i, code in enumerate(target_codes):
        try:
            klines = _fetch_sina_kline(code, period, count)
            if not klines:
                skipped += 1
                if skipped <= 5:
                    logger.debug("No kline data for %s", code)
                time.sleep(delay)
                continue

            bars = _save_klines(conn, code, period, klines, now)
            total_bars += bars
            synced_stocks += 1

            if synced_stocks % 10 == 0:
                pct = (i + 1) / total * 100
                logger.info(
                    "Progress: %d/%d (%.1f%%) — %d stocks, %d bars, %d failed, %d skipped",
                    i + 1, total, pct, synced_stocks, total_bars, failed, skipped,
                )

            # Commit in batches
            if synced_stocks % batch_size == 0:
                conn.commit()
                logger.info("Batch commit at %d stocks, pausing %.0fs...", synced_stocks, batch_pause)
                time.sleep(batch_pause)

            time.sleep(delay)

        except urllib.error.HTTPError as e:
            if e.code == 456:
                # Sina rate limit — back off and retry with longer delay
                logger.warning(
                    "Rate limited at %s (%d/%d), backing off 30s...",
                    code, i + 1, total,
                )
                time.sleep(30)
                try:
                    klines = _fetch_sina_kline(code, period, count)
                    if klines:
                        bars = _save_klines(conn, code, period, klines, now)
                        total_bars += bars
                        synced_stocks += 1
                        time.sleep(delay)
                        continue
                except Exception:
                    pass
            failed += 1
            logger.warning("Failed %s (%d/%d): %s", code, i + 1, total, e)
            time.sleep(delay * 2)
        except Exception as e:
            failed += 1
            logger.warning("Failed %s (%d/%d): %s", code, i + 1, total, e)
            time.sleep(delay)

    conn.commit()
    conn.close()

    return {
        "total": total,
        "synced_stocks": synced_stocks,
        "total_bars": total_bars,
        "failed": failed,
        "skipped": skipped,
    }


# ---------------------------------------------------------------------------
# Fundamentals sync (akshare)
# ---------------------------------------------------------------------------


def sync_stock_basics() -> dict:
    """Sync all A-share stock basic info from akshare."""
    import akshare as ak
    now = datetime.now().isoformat()
    conn = _get_db_conn()
    cursor = conn.cursor()

    logger.info("Fetching stock list from akshare...")
    try:
        df = ak.stock_info_a_code_name()
    except Exception as e:
        logger.error("akshare stock_info_a_code_name failed: %s", e)
        return {"success": False, "message": str(e)}

    count = 0
    for _, row in df.iterrows():
        try:
            code = str(row.get("code", "")).zfill(6)
            name = str(row.get("name", ""))
            if not code or code == "000000":
                continue

            if code.startswith(("688", "689")):
                market = "科创板"
            elif code.startswith("8") or code.startswith("4"):
                market = "北交所"
            elif code.startswith(("600", "601", "603", "605")):
                market = "沪市"
            elif code.startswith(("000", "001", "002", "003")):
                market = "深市"
            else:
                market = "未知"

            cursor.execute("""
                INSERT OR REPLACE INTO stock_basic (code, name, market, updated_at)
                VALUES (?, ?, ?, ?)
            """, (code, name, market, now))
            count += 1
        except Exception as e:
            logger.debug("Failed to insert %s: %s", row, e)

    conn.commit()
    conn.close()
    logger.info("Stock basics: synced %d stocks", count)
    return {"success": True, "synced": count}


def sync_financials(codes: list[str] | None = None, missing_only: bool = False) -> dict:
    """Sync financial indicators using akshare."""
    import akshare as ak
    now = datetime.now().isoformat()
    conn = _get_db_conn()
    cursor = conn.cursor()

    if codes:
        target_codes = [c.strip().zfill(6) for c in codes]
    elif missing_only:
        cursor.execute("""
            SELECT code FROM stock_basic b
            WHERE NOT EXISTS (SELECT 1 FROM stock_financial f WHERE f.code = b.code)
            ORDER BY code
        """)
        target_codes = [r[0] for r in cursor.fetchall()]
    else:
        cursor.execute("SELECT code FROM stock_basic ORDER BY code")
        target_codes = [r[0] for r in cursor.fetchall()]

    total = len(target_codes)
    synced = 0
    failed = 0

    logger.info("Financial sync: %d stocks", total)

    for i, code in enumerate(target_codes):
        try:
            df = ak.stock_financial_abstract(symbol=code)
            if df is None or df.empty:
                continue

            date_cols = [c for c in df.columns if c.isdigit() and len(c) == 8][:4]
            if not date_cols:
                continue

            metric_map = {
                "归母净利润": "net_profit",
                "营业总收入": "revenue",
                "总资产": "total_assets",
                "总负债": "total_liabilities",
            }

            for date_col in date_cols:
                try:
                    row_data = {}
                    for _, row in df.iterrows():
                        metric = row.get("指标", "")
                        if metric in metric_map:
                            val = row.get(date_col)
                            if val is not None:
                                row_data[metric_map[metric]] = float(val)

                    if row_data:
                        cursor.execute("""
                            INSERT OR REPLACE INTO stock_financial
                            (code, report_date, net_profit, revenue,
                             total_assets, total_liabilities, updated_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (
                            code, date_col,
                            row_data.get("net_profit"),
                            row_data.get("revenue"),
                            row_data.get("total_assets"),
                            row_data.get("total_liabilities"),
                            now,
                        ))
                        synced += 1
                except Exception:
                    continue

            if (i + 1) % 10 == 0:
                pct = (i + 1) / total * 100
                logger.info("Financial: %d/%d (%.1f%%), %d rows, %d failed",
                            i + 1, total, pct, synced, failed)
                conn.commit()

        except Exception as e:
            failed += 1
            logger.debug("Financial failed for %s: %s", code, e)

    conn.commit()
    conn.close()

    return {"total": total, "synced": synced, "failed": failed}


def sync_companies(codes: list[str] | None = None, missing_only: bool = False) -> dict:
    """Sync company profiles using akshare."""
    import akshare as ak
    now = datetime.now().isoformat()
    conn = _get_db_conn()
    cursor = conn.cursor()

    if codes:
        target_codes = [c.strip().zfill(6) for c in codes]
    elif missing_only:
        cursor.execute("""
            SELECT code FROM stock_basic b
            WHERE NOT EXISTS (SELECT 1 FROM stock_company c WHERE c.code = b.code)
            ORDER BY code
        """)
        target_codes = [r[0] for r in cursor.fetchall()]
    else:
        cursor.execute("SELECT code FROM stock_basic ORDER BY code")
        target_codes = [r[0] for r in cursor.fetchall()]

    total = len(target_codes)
    synced = 0
    failed = 0

    logger.info("Company sync: %d stocks", total)

    # Get the full stock list for name lookup
    try:
        df_all = ak.stock_info_a_code_name()
    except Exception as e:
        logger.error("Failed to get stock list: %s", e)
        return {"total": total, "synced": 0, "failed": total}

    for i, code in enumerate(target_codes):
        try:
            stock_row = df_all[df_all["code"] == code]
            if stock_row.empty:
                continue

            name = str(stock_row.iloc[0].get("name", ""))
            cursor.execute("""
                INSERT OR REPLACE INTO stock_company
                (code, name, updated_at) VALUES (?, ?, ?)
            """, (code, name, now))
            synced += 1

            if (i + 1) % 100 == 0:
                pct = (i + 1) / total * 100
                logger.info("Company: %d/%d (%.1f%%), %d synced",
                            i + 1, total, pct, synced)

        except Exception as e:
            failed += 1
            logger.debug("Company failed for %s: %s", code, e)

    conn.commit()
    conn.close()
    return {"total": total, "synced": synced, "failed": failed}


# ---------------------------------------------------------------------------
# Daily scheduled sync (run after market close)
# ---------------------------------------------------------------------------

DLY_BARS = 400  # 1.5 years

# Skip sync if the free Sina/akshare APIs are unstable (late-night window).
# These APIs are tuned for trading-hour load and can time out or return empty
# at 2-6 AM Beijing time.
_OFF_PEAK_BLACKOUT_START = dt_time(2, 0)
_OFF_PEAK_BLACKOUT_END = dt_time(6, 0)


def _is_blackout() -> bool:
    now = datetime.now().time()
    return _OFF_PEAK_BLACKOUT_START <= now < _OFF_PEAK_BLACKOUT_END


def daily_sync() -> None:
    """Daily sync run after market close.

    1. Sync stock basics (only if > 30 days since last sync)
    2. Sync K-line for top active stocks
    3. Sync financial data for stocks missing it
    """
    if _is_blackout():
        logger.info("Blackout window (2:00-6:00 Beijing time), skipping daily sync")
        return

    conn = _get_db_conn()
    cursor = conn.cursor()

    # 1. Stock basics — check last update
    cursor.execute("SELECT MAX(updated_at) FROM stock_basic")
    last_basic = cursor.fetchone()[0]
    basic_stale = True
    if last_basic:
        try:
            last_dt = datetime.fromisoformat(last_basic)
            basic_stale = (datetime.now() - last_dt).days > 30
        except ValueError:
            pass

    if basic_stale:
        logger.info("Stock basics stale or missing, syncing...")
        conn.close()
        sync_stock_basics()
        conn = _get_db_conn()
        cursor = conn.cursor()
    else:
        logger.info("Stock basics up to date (%s)", last_basic)

    # 2. K-line — sync ALL stocks missing data (prioritize active ones)
    cursor.execute("""
        SELECT b.code FROM stock_basic b
        LEFT JOIN stock_kline k ON k.code = b.code AND k.period = 'daily'
        GROUP BY b.code
        ORDER BY COUNT(k.code) ASC
    """)
    all_codes = [r[0] for r in cursor.fetchall()]
    conn.close()

    codes_missing = [c for c in all_codes if _count_klines(c, "daily") < 50]

    logger.info("Daily K-line sync: %d stocks need backfill (out of %d total)",
                len(codes_missing), len(all_codes))

    if codes_missing:
        result = sync_klines(
            codes=codes_missing,
            period="daily",
            count=DLY_BARS,
            delay=0.2,
            batch_size=50,
            batch_pause=3.0,
        )
        logger.info("Daily K-line result: %s", json.dumps(result))


def _count_klines(code: str, period: str = "daily") -> int:
    db = _get_db_conn()
    c = db.cursor()
    c.execute("SELECT COUNT(*) FROM stock_kline WHERE code=? AND period=?", (code, period))
    return c.fetchone()[0]


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def show_status() -> None:
    conn = _get_db_conn()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) FROM stock_basic")
    total = cursor.fetchone()[0]

    cursor.execute("SELECT period, COUNT(*) FROM stock_kline GROUP BY period")
    klines = {r[0]: r[1] for r in cursor.fetchall()}

    cursor.execute("SELECT COUNT(DISTINCT code) FROM stock_kline")
    stocks_with_kline = cursor.fetchone()[0]

    cursor.execute("SELECT MIN(date), MAX(date) FROM stock_kline")
    date_range = cursor.fetchone()

    cursor.execute("SELECT COUNT(*) FROM stock_financial")
    fin_rows = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(DISTINCT code) FROM stock_financial")
    stocks_with_fin = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM stock_company")
    company_count = cursor.fetchone()[0]

    cursor.execute("SELECT COUNT(*) FROM stock_basic b WHERE NOT EXISTS (SELECT 1 FROM stock_kline k WHERE k.code = b.code)")
    missing_kline = cursor.fetchone()[0]

    conn.close()

    print(f"Stock database: {_get_db_path()}")
    print(f"  Total stocks:      {total:,}")
    print(f"  K-line records:")
    for period, cnt in sorted(klines.items()):
        print(f"    {period}: {cnt:,} ({stocks_with_kline:,} stocks)")
    print(f"  K-line date range: {date_range[0]} — {date_range[1]}")
    print(f"  Stocks without K-line: {missing_kline:,}")
    print(f"  Financial records: {fin_rows:,} ({stocks_with_fin:,} stocks)")
    print(f"  Company profiles:  {company_count:,}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Stock data sync — sync A-share historical data to local database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="command", help="Sync command")

    # kline
    kline_p = sub.add_parser("kline", help="Sync K-line data")
    group = kline_p.add_mutually_exclusive_group()
    group.add_argument("--all", action="store_true", help="Sync all stocks")
    group.add_argument("--missing", action="store_true", help="Only stocks without data")
    group.add_argument("--codes", type=str, help="Comma-separated stock codes")
    kline_p.add_argument("--period", default="daily", choices=["daily", "weekly", "monthly"])
    kline_p.add_argument("--count", type=int, default=500, help="Bars per stock (max 1000)")
    kline_p.add_argument("--delay", type=float, default=0.5, help="Seconds between requests")
    kline_p.add_argument("--batch-size", type=int, default=50, help="Commit after N stocks")
    kline_p.add_argument("--batch-pause", type=float, default=5.0, help="Pause between batches")

    # financial
    fin_p = sub.add_parser("financial", help="Sync financial data")
    group2 = fin_p.add_mutually_exclusive_group()
    group2.add_argument("--all", action="store_true")
    group2.add_argument("--missing", action="store_true")
    group2.add_argument("--codes", type=str)

    # company
    comp_p = sub.add_parser("company", help="Sync company profiles")
    group3 = comp_p.add_mutually_exclusive_group()
    group3.add_argument("--all", action="store_true")
    group3.add_argument("--missing", action="store_true")
    group3.add_argument("--codes", type=str)

    # basics
    sub.add_parser("basics", help="Sync stock basic list")

    # daily
    sub.add_parser("daily", help="Daily scheduled sync (after market close)")

    # status
    sub.add_parser("status", help="Show database status")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    _init_db_schema(_get_db_conn())

    if args.command == "status":
        show_status()

    elif args.command == "basics":
        result = sync_stock_basics()
        print(json.dumps(result))

    elif args.command == "kline":
        codes = None
        missing_only = False
        if args.codes:
            codes = [c.strip().zfill(6) for c in args.codes.split(",")]
        elif args.missing:
            missing_only = True
        # --all or default: sync everything

        if codes is None and not missing_only:
            logger.warning("No filter specified: syncing ALL stocks. Use --missing for backfill only.")
            logger.warning("Press Ctrl+C within 5s to cancel...")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                print("\nCancelled.")
                sys.exit(0)

        result = sync_klines(
            codes=codes,
            missing_only=missing_only,
            period=args.period,
            count=min(args.count, 1000),
            delay=args.delay,
            batch_size=args.batch_size,
            batch_pause=args.batch_pause,
        )
        print(json.dumps(result))

    elif args.command == "financial":
        codes = None
        missing_only = False
        if args.codes:
            codes = [c.strip().zfill(6) for c in args.codes.split(",")]
        elif args.missing:
            missing_only = True

        result = sync_financials(codes=codes, missing_only=missing_only)
        print(json.dumps(result))

    elif args.command == "company":
        codes = None
        missing_only = False
        if args.codes:
            codes = [c.strip().zfill(6) for c in args.codes.split(",")]
        elif args.missing:
            missing_only = True

        result = sync_companies(codes=codes, missing_only=missing_only)
        print(json.dumps(result))

    elif args.command == "daily":
        daily_sync()


if __name__ == "__main__":
    main()
