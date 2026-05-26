"""
Stock market MCP server — A-share data.

Data sources:
  - Sina Finance: real-time quotes, K-line (fast, free, no auth)
  - akshare: stock search (name/code/pinyin), fundamentals
  - tushare: fallback search + metadata (stock_basic)

Tools:
  stock_search   — search stocks by name or code
  stock_price    — real-time quote for one or more stocks
  stock_kline    — daily/weekly/monthly K-line data
  stock_sync_*   — sync fundamentals to local database
  stock_query_*  — query local database
"""

import asyncio
import json
import logging
import os
import sqlite3
import threading
import time as time_module
import urllib.parse
import urllib.request
from datetime import datetime, time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("stock_mcp")

# ---------------------------------------------------------------------------
# Local database
# ---------------------------------------------------------------------------

_DB_PATH: Path | None = None
_db_conn: sqlite3.Connection | None = None


def _get_db_path() -> Path:
    """Get the database file path in HERMES_HOME."""
    global _DB_PATH
    if _DB_PATH is not None:
        return _DB_PATH
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    _DB_PATH = hermes_home / "stock_data.db"
    return _DB_PATH


def _get_db_conn() -> sqlite3.Connection:
    """Get or create the database connection."""
    global _db_conn
    if _db_conn is not None:
        return _db_conn
    db_path = _get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    _db_conn = sqlite3.connect(db_path, check_same_thread=False)
    _db_conn.row_factory = sqlite3.Row
    _init_db_schema(_db_conn)
    return _db_conn


def _init_db_schema(conn: sqlite3.Connection) -> None:
    """Initialize database schema if not exists."""
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

        CREATE INDEX IF NOT EXISTS idx_financial_code ON stock_financial(code);
        CREATE INDEX IF NOT EXISTS idx_financial_date ON stock_financial(report_date);
        CREATE INDEX IF NOT EXISTS idx_kline_code_period ON stock_kline(code, period);
        CREATE INDEX IF NOT EXISTS idx_kline_date ON stock_kline(date);
    """)
    conn.commit()


def _close_db() -> None:
    """Close database connection."""
    global _db_conn
    if _db_conn is not None:
        _db_conn.close()
        _db_conn = None


# ---------------------------------------------------------------------------
# Data sync functions (akshare)
# ---------------------------------------------------------------------------


def _sync_stock_basics() -> dict:
    """Sync all A-share stock basics to local database via akshare."""
    import akshare as ak
    now = datetime.now().isoformat()

    try:
        # Get all A-share stocks code and name
        df = ak.stock_info_a_code_name()
    except Exception as e:
        logger.warning("akshare stock_info_a_code_name failed: %s", e)
        return {"success": False, "message": f"获取股票列表失败: {e}"}

    conn = _get_db_conn()
    cursor = conn.cursor()
    count = 0

    for _, row in df.iterrows():
        try:
            code = str(row.get("code", "")).zfill(6)
            name = str(row.get("name", ""))
            if not code or code == "000000":
                continue
            # Determine market from code
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
            continue

    conn.commit()
    return {"success": True, "synced": count}


def _sync_stock_financials(codes: list[str] | None = None) -> dict:
    """Sync financial indicators for specified codes (or all if None)."""
    import akshare as ak
    now = datetime.now().isoformat()

    conn = _get_db_conn()
    cursor = conn.cursor()

    # Get stock codes to sync
    if codes is None:
        cursor.execute("SELECT code FROM stock_basic LIMIT 100")
        codes = [r[0] for r in cursor.fetchall()]

    total_synced = 0
    for code in codes[:20]:  # Limit to 20 per call to avoid timeout
        try:
            # Use stock_financial_abstract which returns pivot table format
            df = ak.stock_financial_abstract(symbol=code)
            if df is None or df.empty:
                continue

            # Transform: rows are metrics, columns are dates
            # Find the date columns (most recent 4)
            date_cols = [c for c in df.columns if c.isdigit() and len(c) == 8][:4]
            if not date_cols:
                continue

            # Key metrics we want
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
                            code,
                            date_col,
                            row_data.get("net_profit"),
                            row_data.get("revenue"),
                            row_data.get("total_assets"),
                            row_data.get("total_liabilities"),
                            now,
                        ))
                        total_synced += 1
                except Exception as e:
                    logger.debug("Failed to insert financial for %s date %s: %s", code, date_col, e)
                    continue
        except Exception as e:
            logger.debug("Failed to fetch financial for %s: %s", code, e)
            continue

    conn.commit()
    return {"success": True, "synced": total_synced}


def _sync_stock_companies(codes: list[str] | None = None) -> dict:
    """Sync company profiles for specified codes."""
    import akshare as ak
    now = datetime.now().isoformat()

    conn = _get_db_conn()
    cursor = conn.cursor()

    # Get stock codes to sync
    if codes is None:
        cursor.execute("SELECT code FROM stock_basic LIMIT 100")
        codes = [r[0] for r in cursor.fetchall()]

    total_synced = 0
    for code in codes[:10]:  # Limit to 10 per call (network intensive)
        try:
            # Use stock_info_a_code_name to get basic info
            df = ak.stock_info_a_code_name()
            stock_row = df[df["code"] == code]
            if stock_row.empty:
                continue

            name = str(stock_row.iloc[0].get("name", ""))

            cursor.execute("""
                INSERT OR REPLACE INTO stock_company
                (code, name, introduction, updated_at)
                VALUES (?, ?, ?, ?)
            """, (code, name, "From akshare stock_info", now))
            total_synced += 1
        except Exception as e:
            logger.debug("Failed to fetch company for %s: %s", code, e)
            continue

    conn.commit()
    return {"success": True, "synced": total_synced}

    # Get stock codes to sync
    if codes is None:
        cursor.execute("SELECT code FROM stock_basic LIMIT 100")
        codes = [r[0] for r in cursor.fetchall()]

    total_synced = 0
    for code in codes[:30]:  # Limit to 30 per call
        try:
            df = ak.stock_individual_info_em(symbol=code)
            if df is None or df.empty:
                continue

            # Extract first row as dict
            info = df.iloc[0].to_dict() if len(df) > 0 else {}

            cursor.execute("""
                INSERT OR REPLACE INTO stock_company
                (code, name, chairman, manager, secretary, reg_capital,
                 setup_date, province, city, introduction, website, email, office, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                code,
                info.get("股票简称"),
                info.get("公司全称"),
                info.get("总经理"),
                info.get("董事会秘书"),
                info.get("注册资本"),
                info.get("成立日期"),
                info.get("所在省份"),
                info.get("所在城市"),
                info.get("主营业务"),
                info.get("公司主页"),
                info.get("电子邮箱"),
                info.get("办公地址"),
                now,
            ))
            total_synced += 1
        except Exception as e:
            logger.debug("Failed to fetch company for %s: %s", code, e)
            continue

    conn.commit()
    return {"success": True, "synced": total_synced}


def _sync_stock_klines(codes: list[str] | None = None, period: str = "daily", count: int = 200) -> dict:
    """Sync K-line data for specified stock codes to local database.

    Args:
        codes: List of stock codes. If None, syncs first 20 from stock_basic.
        period: 'daily', 'weekly', or 'monthly'
        count: Number of bars to fetch (max 1000 per call)
    """
    now = datetime.now().isoformat()
    conn = _get_db_conn()
    cursor = conn.cursor()

    # Get stock codes to sync
    if codes is None:
        cursor.execute("SELECT code FROM stock_basic LIMIT 20")
        codes = [r[0] for r in cursor.fetchall()]

    # Limit count to max 1000 per stock
    count = min(count, 1000)

    total_synced = 0
    for code in codes[:len(codes)]:  # Process all provided codes
        try:
            klines = _fetch_sina_kline(code, period, count)
            if not klines:
                continue

            for bar in klines:
                cursor.execute("""
                    INSERT OR REPLACE INTO stock_kline
                    (code, period, date, open, high, low, close, volume, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    code,
                    period,
                    bar.get("date"),
                    bar.get("open"),
                    bar.get("high"),
                    bar.get("low"),
                    bar.get("close"),
                    bar.get("volume"),
                    now,
                ))
                total_synced += 1
        except Exception as e:
            logger.debug("Failed to sync kline for %s: %s", code, e)
            continue

    conn.commit()
    return {"success": True, "period": period, "synced": total_synced}


def _query_stock_kline(code: str, period: str = "daily", limit: int = 60) -> dict:
    """Query K-line data from local database.

    Args:
        code: Stock code (e.g. '000001')
        period: 'daily', 'weekly', or 'monthly'
        limit: Max number of bars to return (most recent)
    """
    conn = _get_db_conn()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT date, open, high, low, close, volume
        FROM stock_kline
        WHERE code = ? AND period = ?
        ORDER BY date DESC
        LIMIT ?
    """, (code.zfill(6), period, limit))

    rows = cursor.fetchall()
    if not rows:
        return {"success": False, "message": f"No kline data for {code} ({period})"}

    # Return in chronological order
    bars = [
        {
            "date": r[0],
            "open": r[1],
            "high": r[2],
            "low": r[3],
            "close": r[4],
            "volume": r[5],
        }
        for r in rows
    ][::-1]  # Reverse to chronological

    return {
        "success": True,
        "code": code,
        "period": period,
        "count": len(bars),
        "bars": bars,
    }


def _query_stock_basic(code: str | None = None, keyword: str | None = None, limit: int = 10) -> dict:
    """Query stock basic info from local database."""
    conn = _get_db_conn()
    cursor = conn.cursor()

    if code:
        cursor.execute("SELECT * FROM stock_basic WHERE code = ?", (code.zfill(6),))
        row = cursor.fetchone()
        if row:
            return {"success": True, "stock": dict(row)}
        return {"success": False, "message": f"未找到股票: {code}"}

    if keyword:
        cursor.execute(
            "SELECT * FROM stock_basic WHERE name LIKE ? OR code LIKE ? LIMIT ?",
            (f"%{keyword}%", f"%{keyword}%", limit)
        )
    else:
        cursor.execute("SELECT * FROM stock_basic LIMIT ?", (limit,))

    rows = cursor.fetchall()
    stocks = [dict(r) for r in rows]
    return {"success": True, "count": len(stocks), "stocks": stocks}


# ---------------------------------------------------------------------------
# Scheduler for periodic sync
# ---------------------------------------------------------------------------

_scheduler_thread: threading.Thread | None = None
_stop_scheduler = threading.Event()


def _run_scheduler() -> None:
    """Background scheduler for periodic data sync.

    Runs after market close (16:10) to avoid hitting free Sina/akshare
    APIs during peak trading hours, which are unstable under load.
    """
    last_basic_sync = ""
    last_kline_sync = ""
    last_financial_sync = ""

    while not _stop_scheduler.is_set():
        try:
            now = datetime.now()
            current_date = now.strftime("%Y-%m-%d")
            current_hour = now.hour
            current_minute = now.minute

            # 16:10 - Stock basics (daily, after market close)
            if current_hour == 16 and 10 <= current_minute < 20 and last_basic_sync != current_date:
                logger.info("Running scheduled stock basics sync (post-market)...")
                _sync_stock_basics()
                last_basic_sync = current_date

            # 16:20 - K-line data for top stocks (daily, after market close)
            if current_hour == 16 and 20 <= current_minute < 30 and last_kline_sync != current_date:
                logger.info("Running scheduled K-line sync (post-market)...")
                top_stocks = _get_top_stocks(50)
                _sync_stock_klines(top_stocks, "daily", 200)
                last_kline_sync = current_date

            # 17:00 - Financial data (weekly on Sunday)
            if current_hour == 17 and current_minute < 10:
                if now.weekday() == 6:  # Sunday
                    if last_financial_sync != current_date:
                        logger.info("Running scheduled financial sync (post-market)...")
                        _sync_stock_financials()
                        last_financial_sync = current_date

        except Exception as e:
            logger.debug("Scheduler error: %s", e)
        time_module.sleep(60)


def _get_top_stocks(limit: int = 20) -> list[str]:
    """Get list of popular stock codes for scheduled sync."""
    conn = _get_db_conn()
    cursor = conn.cursor()
    # For now, just get first N stocks (could be improved with market cap data)
    cursor.execute("SELECT code FROM stock_basic LIMIT ?", (limit,))
    return [r[0] for r in cursor.fetchall()]


def _start_scheduler() -> None:
    """Start the background scheduler thread."""
    global _scheduler_thread
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        return
    _stop_scheduler.clear()
    _scheduler_thread = threading.Thread(target=_run_scheduler, daemon=True)
    _scheduler_thread.start()
    logger.info("Stock data scheduler started")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TRADING_MORNING_START = time(9, 30)
_TRADING_MORNING_END = time(11, 30)
_TRADING_AFTERNOON_START = time(13, 0)
_TRADING_AFTERNOON_END = time(15, 0)

_SINA_SCALE = {"daily": 240, "weekly": 1200, "monthly": 7200}

_HTTP_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": "Mozilla/5.0 (compatible; StockMCP/2.0)",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_trading_hours() -> bool:
    """Check if we are in A-share trading hours (Beijing time)."""
    now = datetime.now()
    if now.weekday() >= 5:
        return False
    t = now.time()
    if _TRADING_MORNING_START <= t <= _TRADING_MORNING_END:
        return True
    if _TRADING_AFTERNOON_START <= t <= _TRADING_AFTERNOON_END:
        return True
    return False


def _resolve_sina_code(code: str) -> str:
    """Convert stock code to Sina format: sz000001 or sh600519."""
    c = code.strip().lower()
    # Strip tushare-style suffix (.sh/.sz)
    c = c.removesuffix(".sh").removesuffix(".sz")
    if c.startswith(("sz", "sh")):
        return c
    c = c.zfill(6)
    if c.startswith(("6", "9")):
        return f"sh{c}"
    return f"sz{c}"


def _http_get(url: str, timeout: int = 8) -> str:
    """Synchronous HTTP GET returning decoded text."""
    req = urllib.request.Request(url, headers=_HTTP_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        charset = resp.headers.get_content_charset() or "gbk"
        return raw.decode(charset, errors="replace")


def _http_post_json(url: str, data: dict, timeout: int = 8) -> dict:
    """Synchronous HTTP POST JSON, returns parsed dict."""
    req = urllib.request.Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ---------------------------------------------------------------------------
# tushare token loader
# ---------------------------------------------------------------------------

_SENTINEL = object()
_TUSHARE_TOKEN = _SENTINEL


def _load_tushare_token() -> str:
    """Load tushare token from TUSHARE_TOKEN env or .mcp.json."""
    global _TUSHARE_TOKEN
    if _TUSHARE_TOKEN is not _SENTINEL:
        return _TUSHARE_TOKEN  # type: ignore[return-value]

    token = os.environ.get("TUSHARE_TOKEN", "")
    if token:
        _TUSHARE_TOKEN = token
        return token

    mcp_file = (
        Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")) / ".mcp.json"
    )
    try:
        if mcp_file.exists():
            data = json.loads(mcp_file.read_text())
            url = data.get("mcpServers", {}).get("tushareMcp", {}).get("url", "")
            params = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            token = params.get("token", [""])[0]
            if token:
                _TUSHARE_TOKEN = token
                return token
    except Exception:
        pass
    _TUSHARE_TOKEN = ""
    return ""


# ---------------------------------------------------------------------------
# Sina: real-time quotes
# ---------------------------------------------------------------------------


def _parse_sina_price(text: str) -> list[dict]:
    """Parse Sina Finance real-time quote response into stock dicts."""
    stocks = []
    for line in text.strip().split("\n"):
        if not line.strip():
            continue
        # Format: var hq_str_sz000001="name,open,pre_close,price,...";
        parts = line.split('"')
        if len(parts) < 2:
            continue
        fields = parts[1].split(",")
        if len(fields) < 32:
            continue
        var_name = line.split("=")[0].strip()
        sina_code = var_name.replace("var hq_str_", "")
        code = sina_code[2:]

        try:
            stocks.append({
                "code": code,
                "name": fields[0],
                "price": float(fields[3]) if fields[3] else None,
                "change": round(float(fields[3]) - float(fields[2]), 4)
                if fields[3] and fields[2] else None,
                "change_pct": round(
                    (float(fields[3]) - float(fields[2])) / float(fields[2]) * 100, 2
                ) if fields[3] and fields[2] and float(fields[2]) != 0 else None,
                "open": float(fields[1]) if fields[1] else None,
                "high": float(fields[4]) if fields[4] else None,
                "low": float(fields[5]) if fields[5] else None,
                "pre_close": float(fields[2]) if fields[2] else None,
                "volume": int(fields[8]) if fields[8] else None,
                "turnover": float(fields[9]) if fields[9] else None,
            })
        except (ValueError, IndexError):
            continue
    return stocks


# ---------------------------------------------------------------------------
# Sina: K-line
# ---------------------------------------------------------------------------


def _fetch_sina_kline(code: str, period: str, count: int = 500) -> list[dict]:
    """Fetch K-line data from Sina Finance."""
    sina_code = _resolve_sina_code(code)
    scale = _SINA_SCALE.get(period, 240)
    # Sina API max is 1000
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


# ---------------------------------------------------------------------------
# Stock search: akshare -> tushare fallback
# ---------------------------------------------------------------------------

# In-memory stock list cache (refreshed every 6 hours)
_stock_list_cache: tuple[list[dict], float] | None = None
_STOCK_LIST_CACHE_TTL = 21600  # 6 hours


def _get_cached_stock_list() -> list[dict]:
    """Return the full A-share stock list from cache, or load + cache it."""
    global _stock_list_cache
    now = time.time()
    if _stock_list_cache is not None:
        data, cached_at = _stock_list_cache
        if now - cached_at < _STOCK_LIST_CACHE_TTL:
            return data
    import akshare as ak
    import time as _time
    df = ak.stock_info_a_code_name()
    data = [
        {"code": r["code"], "name": r["name"]}
        for _, r in df.iterrows()
    ]
    _stock_list_cache = (data, _time.time())
    return data


def _search_akshare(query: str) -> list[dict]:
    """Search via cached akshare stock list. Returns [{code, name}]."""
    stock_list = _get_cached_stock_list()
    q = query.strip()
    results = []
    for s in stock_list:
        if q in s["name"] or s["code"].startswith(q):
            results.append(s)
            if len(results) >= 10:
                break
    return results


def _search_tushare(query: str) -> list[dict]:
    """Search via tushare stock_basic. Returns [{code, name, industry, market}]."""
    token = _load_tushare_token()
    if not token:
        return []
    result = _http_post_json("http://api.tushare.pro", {
        "api_name": "stock_basic",
        "token": token,
        "params": {"name": query},
        "fields": "ts_code,name,industry,market",
    })
    if result.get("code") != 0:
        return []
    items = result.get("data", {}).get("items", []) or []
    return [
        {"code": it[0], "name": it[1], "industry": it[2], "market": it[3]}
        for it in items[:10]
    ]


# ---------------------------------------------------------------------------
# MCP Server
# ---------------------------------------------------------------------------

mcp = FastMCP("stock-mcp")


# NOTE: individual tools disabled in favor of stock_analyze which combines
# search + price + kline into a single round-trip.  Keep implementations
# as plain functions — stock_analyze calls them internally.
async def stock_search(keyword: str) -> str:
    """Search A-share stocks by name, code, or pinyin.

    Args:
        keyword: Stock name, code prefix, or pinyin (e.g. 'pingan', '000001')
    """
    loop = asyncio.get_running_loop()

    # Primary: akshare (supports pinyin, name, code matching)
    try:
        results = await loop.run_in_executor(None, _search_akshare, keyword)
        if results:
            return json.dumps(
                {"success": True, "count": len(results), "stocks": results},
                ensure_ascii=False,
            )
    except Exception as e:
        logger.debug("akshare search failed: %s", e)

    # Fallback: tushare stock_basic (name-based search)
    try:
        results = await loop.run_in_executor(None, _search_tushare, keyword)
        if results:
            return json.dumps(
                {"success": True, "count": len(results), "stocks": results},
                ensure_ascii=False,
            )
    except Exception as e:
        logger.debug("tushare search failed: %s", e)

    return json.dumps(
        {"success": False, "message": f"No stocks found for: {keyword}"},
        ensure_ascii=False,
    )


async def stock_price(codes: str) -> str:
    """Get real-time quotes for one or more A-share stocks.

    Args:
        codes: Comma-separated stock codes like '000001,600519,sz002594'.
    """
    parts = [p.strip() for p in codes.split(",") if p.strip()]
    if not parts:
        return json.dumps(
            {"success": False, "message": "No stock codes provided"},
            ensure_ascii=False,
        )

    sina_codes = [_resolve_sina_code(p) for p in parts]
    url = f"https://hq.sinajs.cn/list={','.join(sina_codes)}"

    try:
        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, _http_get, url, 8)
        stocks = _parse_sina_price(text)
        return json.dumps(
            {"success": True, "count": len(stocks), "stocks": stocks},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "message": f"行情获取失败: {e}"},
            ensure_ascii=False,
        )



async def stock_kline(code: str, period: str = "daily", count: int = 60) -> str:
    """Get K-line (candlestick) data for a stock.

    Args:
        code: Stock code like '000001', '600519'.
        period: 'daily', 'weekly', or 'monthly'.
        count: Number of bars (max 200).
    """
    if period not in _SINA_SCALE:
        return json.dumps(
            {"success": False,
             "message": f"Invalid period: {period}. Use daily/weekly/monthly."},
            ensure_ascii=False,
        )

    try:
        loop = asyncio.get_running_loop()
        klines = await loop.run_in_executor(
            None, _fetch_sina_kline, code, period, min(count, 200)
        )
        return json.dumps(
            {"code": code, "period": period, "count": len(klines), "bars": klines},
            ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps(
            {"success": False, "message": f"K线获取失败: {e}"},
            ensure_ascii=False,
        )


@mcp.tool()
async def stock_analyze(keyword: str) -> str:
    """Search a stock by name/code and return quote + recent K-line in one call.

    Use this for stock analysis queries. It combines search, price, and
    daily K-line into a single response — much faster than calling each
    tool separately.

    Args:
        keyword: Stock name, code, or pinyin (e.g. '茅台', '600519', 'pingan')
    """
    loop = asyncio.get_running_loop()

    # Step 1: search (cached, fast after first load)
    try:
        results = await loop.run_in_executor(None, _search_akshare, keyword)
    except Exception as e:
        logger.debug("akshare search failed in analyze: %s", e)
        results = []

    if not results:
        try:
            results = await loop.run_in_executor(None, _search_tushare, keyword)
        except Exception:
            results = []

    if not results:
        return json.dumps(
            {"success": False, "message": f"未找到相关股票: {keyword}"},
            ensure_ascii=False,
        )

    # Take top match — strip tushare-style suffix (.SH/.SZ)
    stock = results[0]
    code = stock["code"].split(".")[0] if "." in stock["code"] else stock["code"]
    name = stock["name"]
    candidates = [r["code"] for r in results[:3]]

    # Step 2: price (Sina, fast)
    price_data = None
    try:
        price_result = json.loads(await stock_price(code))
        if price_result.get("success"):
            price_data = price_result.get("stocks", [{}])[0] if price_result.get("stocks") else None
    except Exception:
        pass

    # Step 3: daily K-line (Sina, fast)
    kline_data = None
    try:
        kline_result = json.loads(await stock_kline(code, "daily", 15))
        if kline_result.get("success", True):
            kline_data = kline_result.get("bars")
    except Exception:
        pass

    return json.dumps({
        "success": True,
        "code": code,
        "name": name,
        "price": price_data,
        "kline_daily": kline_data,
        "also_matched": candidates[1:],
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Local database sync tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def stock_sync_basic() -> str:
    """Sync all A-share stock basic info to local database.

    Fetches stock code, name, and market from akshare and stores locally.
    Run this periodically to keep the local database up to date.
    """
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, _sync_stock_basics)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def stock_sync_financial(codes: str = "") -> str:
    """Sync financial indicators for specified stock codes.

    Args:
        codes: Comma-separated stock codes. If empty, syncs first 50 from DB.
    """
    code_list = [c.strip().zfill(6) for c in codes.split(",") if c.strip()] if codes else None
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: _sync_stock_financials(code_list))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def stock_sync_company(codes: str = "") -> str:
    """Sync company profiles for specified stock codes.

    Args:
        codes: Comma-separated stock codes. If empty, syncs first 30 from DB.
    """
    code_list = [c.strip().zfill(6) for c in codes.split(",") if c.strip()] if codes else None
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(None, lambda: _sync_stock_companies(code_list))
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def stock_query_basic(code: str = "", keyword: str = "", limit: int = 10) -> str:
    """Query stock basic info from local database.

    Args:
        code: Stock code (e.g. '000001')
        keyword: Search by name (e.g. '平安')
        limit: Max results for keyword search (default 10)
    """
    result = _query_stock_basic(code=code, keyword=keyword, limit=limit)
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def stock_sync_kline(codes: str = "", period: str = "daily", count: int = 200) -> str:
    """Sync K-line data to local database.

    Fetches historical K-line data from Sina and stores locally.
    Run this periodically to build local history.

    Args:
        codes: Comma-separated stock codes. If empty, syncs first 20 from DB.
        period: 'daily', 'weekly', or 'monthly' (default: daily)
        count: Number of bars to fetch per stock (max 200)
    """
    code_list = [c.strip().zfill(6) for c in codes.split(",") if c.strip()] if codes else None
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: _sync_stock_klines(code_list, period, count)
    )
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def stock_query_kline(code: str, period: str = "daily", limit: int = 60) -> str:
    """Query K-line data from local database.

    Args:
        code: Stock code (e.g. '000001', '600519')
        period: 'daily', 'weekly', or 'monthly' (default: daily)
        limit: Max number of bars (default 60)
    """
    result = _query_stock_kline(code=code, period=period, limit=limit)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    # Start background scheduler
    _start_scheduler()
    mcp.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
