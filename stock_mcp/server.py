"""
Stock market MCP server — A-share data.

Data sources:
  - Sina Finance: real-time quotes, K-line (fast, free, no auth)
  - akshare: stock search (name/code/pinyin)
  - tushare: fallback search + metadata (stock_basic)

Tools:
  stock_search   — search stocks by name or code
  stock_price    — real-time quote for one or more stocks
  stock_kline    — daily/weekly/monthly K-line data
"""

import asyncio
import json
import logging
import os
import urllib.parse
import urllib.request
from datetime import datetime, time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("stock_mcp")

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


def _fetch_sina_kline(code: str, period: str, count: int) -> list[dict]:
    """Fetch K-line data from Sina Finance."""
    sina_code = _resolve_sina_code(code)
    scale = _SINA_SCALE.get(period, 240)
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


def _search_akshare(query: str) -> list[dict]:
    """Search via akshare stock list. Returns [{code, name}]."""
    import akshare as ak

    df = ak.stock_info_a_code_name()
    q = query.strip()
    mask = (
        df["name"].str.contains(q, na=False)
        | df["code"].str.startswith(q, na=False)
    )
    results = df[mask].head(10)
    return [
        {"code": r["code"], "name": r["name"]}
        for _, r in results.iterrows()
    ]


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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    mcp.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
