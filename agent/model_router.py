"""AI model routing by user tier and query complexity.

Routes queries to different models based on:
- User level (free / vip)
- Query complexity (keyword matching for financial-domain terms)

Uses the centralized call_llm() from auxiliary_client as transport.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agent.auxiliary_client import call_llm

logger = logging.getLogger(__name__)

# ── Config ──────────────────────────────────────────────────────────────────

COMPLEX_KEYWORDS = (
    "研报", "公告", "财报", "政策影响", "行业分析", "持仓分析", "组合优化",
)

# (provider, model) pairs
ROUTE_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    # (user_level, query_type) → (provider, model)
    ("free", "simple"):  ("custom", "deepseek-v4-flash"),
    ("free", "complex"): ("custom", "deepseek-v4-flash"),
    ("vip",  "simple"):  ("custom", "deepseek-v4-flash"),
    ("vip",  "complex"): ("custom", "deepseek-v4-flash"),
}

# Fallback: same-tier alternative when primary fails
FALLBACK_TABLE: dict[tuple[str, str], tuple[str, str]] = {
    ("custom", "deepseek-v4-flash"): ("custom", "deepseek-v4-pro"),
}


@dataclass
class RouteResult:
    content: str
    model_used: str
    cost: float


# ── Core ────────────────────────────────────────────────────────────────────

def classify_query(text: str) -> str:
    """Return 'complex' if text hits any financial keyword, else 'simple'."""
    for kw in COMPLEX_KEYWORDS:
        if kw in text:
            return "complex"
    return "simple"


def select_model(user_level: str, query_type: str) -> tuple[str, str]:
    """Return (provider, model) for the given user level and query type."""
    key = (user_level, query_type)
    return ROUTE_TABLE.get(key, ROUTE_TABLE[("free", "simple")])


def _estimate_cost(model: str, content: str) -> float:
    """Rough cost estimate. Override with real pricing data if available."""
    # Placeholder: $0.001 per 1k tokens, ~4 chars per token
    tokens = len(content) / 4
    return round(tokens / 1000 * 0.001, 6)


def route(user_level: str, query: str) -> RouteResult:
    """Main entry point. Classify, route, call with one-retry fallback.

    Args:
        user_level: 'free' or 'vip'.
        query: User's input text.

    Returns:
        RouteResult with content, model_used, cost.

    Raises:
        RuntimeError: If all attempts fail.
    """
    query_type = classify_query(query)
    primary = select_model(user_level, query_type)
    fallback = FALLBACK_TABLE.get(primary)
    candidates = [primary] + ([fallback] if fallback else [])

    last_exc: Exception | None = None
    for provider, model in candidates:
        try:
            messages = [{"role": "user", "content": query}]
            resp = call_llm(provider=provider, model=model, messages=messages)
            content = resp.choices[0].message.content
            return RouteResult(
                content=content,
                model_used=f"{provider}/{model}",
                cost=_estimate_cost(model, content),
            )
        except Exception as exc:
            logger.warning("Model %s/%s failed: %s — trying fallback", provider, model, exc)
            last_exc = exc

    raise RuntimeError(f"All models failed for query: {query[:80]}") from last_exc
