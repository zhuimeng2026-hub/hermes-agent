"""
Stock market MCP server — real-time A-share data via Eastmoney API.

Tools:
  stock_search   — search stocks by name or code
  stock_price    — real-time quote for one or more stocks
  stock_kline    — daily/weekly/monthly K-line data
"""

import json
import logging
import asyncio
from datetime import datetime, time
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

logger = logging.getLogger("stock_mcp")

# A股交易时段（北京时间）
_TRADING_MORNING_START = time(9, 30)
_TRADING_MORNING_END = time(11, 30)
_TRADING_AFTERNOON_START = time(13, 0)
_TRADING_AFTERNOON_END = time(15, 0)


def _is_trading_hours() -> bool:
    now = datetime.now()
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = now.time()
    if _TRADING_MORNING_START <= t <= _TRADING_MORNING_END:
        return True
    if _TRADING_AFTERNOON_START <= t <= _TRADING_AFTERNOON_END:
        return True
    return False


def _error_message(e: Exception, context: str = "") -> str:
    """将异常映射为用户友好的结构化提示，供 LLM 准确理解错误原因。"""
    if isinstance(e, httpx.TimeoutException):
        return "股票数据请求超时，当前网络可能不稳定或 Eastmoney 接口响应较慢"
    if isinstance(e, httpx.ConnectError):
        return "暂时无法连接股票数据服务器，请稍后重试"
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        if status == 404:
            return "未找到该股票数据，可能代码有误或已下市"
        if status in (502, 503, 504):
            return "股票数据服务器暂时不可用（网关异常），请稍后重试"
        if status == 403:
            return "股票数据接口访问被拒绝，请稍后重试"
        return f"股票数据接口返回错误（HTTP {status}），请稍后重试"
    if "certificate" in str(e).lower() or "ssl" in str(e).lower():
        return "SSL 证书错误，请稍后重试"
    # 未知异常，附带上下文判断是否可能为休市
    if not _is_trading_hours():
        return "当前非交易时段，行情数据暂停服务（非交易时间：工作日 9:30-11:30 / 13:00-15:00）"
    return f"股票数据获取失败：{type(e).__name__} {str(e)}"


mcp = FastMCP("stock-mcp")

# ---------------------------------------------------------------
# Eastmoney fields
# ---------------------------------------------------------------
_PRICE_FIELDS = (
    "f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f14,f15,f16,f17,f18,f20,f21,"
    "f22,f23,f24,f25,f38,f40,f43,f44,f45,f46,f48,f50,f57,f58,f60,"
    "f116,f117,f168,f170,f171"
)


async def _get(url: str, params: dict) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, params=params)
        r.raise_for_status()
        return r.json()


def _resolve_secid(query: str) -> str:
    q = query.strip().lower()
    if q.startswith("sh"):
        return f"1.{q[2:]}"
    if q.startswith("sz"):
        return f"0.{q[2:]}"
    code = q.zfill(6)
    if code.startswith(("6", "9")):
        return f"1.{code}"
    return f"0.{code}"


async def _search(query: str) -> list[dict]:
    try:
        data = await _get(
            "https://searchadapter.eastmoney.com/api/suggest/get",
            {"input": query, "type": 14, "token": "D43BF722C8E33BDC906FB84D85E326E8", "count": 10},
        )
        results = []
        for item in data.get("QuotationCodeTable", {}).get("Data", [])[:10]:
            results.append({
                "code": item.get("Code", ""),
                "name": item.get("Name", ""),
                "market": item.get("Market", ""),
                "pinyin": item.get("PinYin", ""),
            })
        return results
    except Exception:
        logger.debug("Search API failed", exc_info=True)
    # Fallback
    try:
        secid = _resolve_secid(query)
        info = await _get(
            "https://push2.eastmoney.com/api/qt/stock/get",
            {"secid": secid, "fields": "f12,f14,f57,f58"},
        )
        d = info.get("data") or {}
        if d.get("f12"):
            return [{"code": d["f12"], "name": d.get("f14", ""), "market": "1" if secid.startswith("1.") else "0"}]
    except Exception:
        pass
    return []


# ---------------------------------------------------------------
# Tools
# ---------------------------------------------------------------

@mcp.tool()
async def stock_search(keyword: str) -> str:
    """Search A-share stocks by name, code, or pinyin. Returns matching stock codes and names."""
    results = await _search(keyword)
    if not results:
        return json.dumps({"success": False, "message": f"No stocks found for: {keyword}"}, ensure_ascii=False)
    return json.dumps({"success": True, "count": len(results), "stocks": results}, ensure_ascii=False)


@mcp.tool()
async def stock_price(codes: str) -> str:
    """Get real-time quotes for one or more A-share stocks.

    Args:
        codes: Comma-separated stock codes like '000001,600519,sz002594'.
    """
    parts = [p.strip() for p in codes.split(",") if p.strip()]
    if not parts:
        return json.dumps({"success": False, "message": "No stock codes provided"}, ensure_ascii=False)

    secids = [_resolve_secid(p) for p in parts]
    try:
        data = await _get(
            "https://push2.eastmoney.com/api/qt/ulist.np/get",
            {"fltt": 2, "secids": ",".join(secids), "fields": _PRICE_FIELDS},
        )
        items = (data.get("data") or {}).get("diff") or []
        stocks = []
        for s in items:
            stocks.append({
                "code": s.get("f12", ""),
                "name": s.get("f14", ""),
                "price": s.get("f2"),
                "change": s.get("f4"),
                "change_pct": s.get("f3"),
                "open": s.get("f17"),
                "high": s.get("f15"),
                "low": s.get("f16"),
                "pre_close": s.get("f18"),
                "volume": s.get("f5"),
                "turnover": s.get("f6"),
                "turnover_rate": s.get("f8"),
                "pe": s.get("f9"),
                "market_cap": s.get("f20"),
                "circ_cap": s.get("f21"),
                "amplitude": s.get("f7"),
                "quantity_ratio": s.get("f50"),
            })
        return json.dumps({"success": True, "count": len(stocks), "stocks": stocks}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": _error_message(e)}, ensure_ascii=False)


@mcp.tool()
async def stock_kline(code: str, period: str = "daily", count: int = 60) -> str:
    """Get K-line (candlestick) data for a stock.

    Args:
        code: Stock code like '000001', '600519'.
        period: 'daily', 'weekly', or 'monthly'.
        count: Number of bars (max 200).
    """
    period_map = {"daily": 101, "weekly": 102, "monthly": 103}
    klt = period_map.get(period, 101)
    secid = _resolve_secid(code)
    try:
        data = await _get(
            "https://push2his.eastmoney.com/api/qt/stock/kline/get",
            {
                "secid": secid,
                "klt": klt,
                "lmt": min(count, 200),
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57",
                "fqt": 1,
            },
        )
        raw = (data.get("data") or {}).get("klines") or []
        klines = []
        for line in raw:
            parts = line.split(",")
            if len(parts) >= 7:
                klines.append({
                    "date": parts[0],
                    "open": float(parts[1]),
                    "close": float(parts[2]),
                    "high": float(parts[3]),
                    "low": float(parts[4]),
                    "volume": float(parts[5]),
                    "turnover": float(parts[6]),
                })
        return json.dumps({"code": code, "period": period, "count": len(klines), "bars": klines}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"success": False, "message": _error_message(e)}, ensure_ascii=False)


# ---------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------

def main():
    mcp.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    main()
