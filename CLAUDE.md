# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Hermes Agent — an open-source AI agent framework by Nous Research. Self-improving agent with tool calling, persistent memory, multi-platform messaging gateway, skills system, and MCP integration. Provider-agnostic: works with any OpenAI-compatible endpoint, Anthropic, Bedrock, Gemini, and 15+ other providers.

## Build, test, lint

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run tests (skips integration tests, runs in parallel)
pytest

# Run all tests including integration (needs API keys)
pytest -m ''

# Run a single test file
pytest tests/test_model_tools.py

# Type checking
ty

# Linting (currently all rules disabled — being wrangled)
ruff check
```

## Entry points (from `pyproject.toml`)

| Command | Module | Purpose |
|---------|--------|---------|
| `hermes` | `hermes_cli.main:main` | Interactive CLI, gateway management, setup wizard |
| `hermes-agent` | `run_agent:main` | Direct agent runner (programmatic usage) |
| `hermes-acp` | `acp_adapter.entry:main` | ACP server for editor integration |

## Architecture

### Agent core (`run_agent.py`)

`AIAgent` class is the central agent loop. It manages the OpenAI-compatible client, tool calling loop, message history, and response handling. Key flow:

1. `run_conversation()` receives user message, builds context (system prompt, memory, skills, subdirectory hints, kanban guidance)
2. Sends to LLM provider via the configured transport (default: OpenAI chat completions)
3. If the model returns tool calls, dispatches them via `model_tools.handle_function_call()` which routes to `tools.registry.registry.dispatch()`
4. Loops until the model returns a text response or `max_turns` is reached

### Tool system (`tools/registry.py` + `model_tools.py` + `toolsets.py`)

Tools self-register at module import time via `registry.register(name, toolset, schema, handler, ...)`. The `ToolRegistry` singleton collects all tools. `model_tools.get_tool_definitions()` queries the registry to build OpenAI-format function schemas, filtered by enabled toolsets and availability checks.

`toolsets.py` defines tool groupings — `_HERMES_CORE_TOOLS` is the shared tool list for CLI and all messaging platforms. Toolsets can compose other toolsets.

### Agent internals (`agent/`)

- `agent/memory_manager.py` — Streaming context scrubbing and memory context block building
- `agent/context_compressor.py` — Context compression when approaching token limits
- `agent/curator.py` — Skill learning: creates/revisits skills from conversation experience
- `agent/prompt_builder.py` — System prompt assembly (identity, platform hints, memory, skills, kanban guidance)
- `agent/error_classifier.py` — API error classification with failover reasoning
- `agent/model_metadata.py` — Model metadata fetching, token estimation, context limit parsing
- `agent/transports/` — Transport adapters (Anthropic native, Bedrock, chat completions, Codex)

### Gateway (`gateway/`)

Messaging gateway supporting Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Email, and 10+ other platforms.

- `gateway/config.py` — `GatewayConfig` dataclass: platform configs, session policies, delivery settings, `Platform` enum
- `gateway/run.py` — `start_gateway()` async function: initializes adapters, starts cron ticker, manages session lifecycle
- `gateway/platforms/` — One adapter per platform (each extends `BasePlatformAdapter`)
- `gateway/platforms/api_server.py` — OpenAI-compatible HTTP API on port 8642 (`/v1/chat/completions`, `/v1/responses`, `/v1/models`) plus stock data endpoints (`/api/stock/basic`, `/api/stock/daily`, etc.)
- `gateway/session.py` — Session management with SessionDB (SQLite + FTS5)
- `gateway/delivery.py` — Message delivery pipeline

### MCP server (`mcp_serve.py`)

Exposes Hermes messaging as 10 MCP tools: `conversations_list`, `conversation_get`, `messages_read`, `attachments_fetch`, `events_poll`, `events_wait`, `messages_send`, `channels_list`, `permissions_list_open`, `permissions_respond`. Uses `EventBridge` to poll SessionDB for new messages.

### Database (`hermes_state.py`)

SQLite database at `~/.hermes/state.db` with WAL mode. `SessionDB` class manages:
- `sessions` — session metadata (source, model, timestamps, token counts)
- `messages` — message history (role, content, timestamp)
- FTS5 full-text search on messages
- Thread-safe with write retry logic for concurrent access

### Stock data system

- `stock_mcp/server.py` — MCP server for A-share data (Sina Finance for quotes/K-line, akshare for search/fundamentals, tushare fallback). Tool: `stock_analyze` (search + price + kline in one call) plus sync/query tools.
- `stock_mcp/sync.py` — CLI sync script for batch backfilling historical K-line and financial data to `stock_data.db` with rate-limit handling (HTTP 456 backoff). Cron-friendly.
- `stock_data.db` — Local SQLite at `~/.hermes/stock_data.db`: `stock_basic`, `stock_kline`, `stock_financial`, `stock_company`.
- `gateway/platforms/api_server.py` — REST endpoints at `/api/stock/*` (basic, daily, daily_basic, income, balancesheet, cashflow) proxying tushare HTTP API.

### Plugins (`plugins/`)

Plugin system with model providers (`plugins/model-providers/`), memory backends (`plugins/memory/`), and platform adapters (`plugins/platforms/`).

### Config

Managed at `/opt/hermes-agent/configs/config.yaml` (symlinked from `~/.hermes/config.yaml`). Multi-model config: primary model, delegation model, auxiliary models (vision, web_extract), fallback providers. MCP servers are declared under `mcp_servers`.

## This deployment

Running on a Linux VPS as root with `HERMES_HOME=/root/.hermes`. Uses a custom OpenAI-compatible endpoint at `aikey.aixifs.com`. Default model: `qwen-plus`. Vision model: `qwen-vl-max`. Fallback: `deepseek-v4-pro`. Stock MCP server configured. Stock data cron sync runs weekdays at 16:30 (K-line), Fridays 17:00 (financials), monthly basics refresh.

## Key patterns

- **Lazy imports**: `run_agent.py` uses a proxy pattern for `openai.OpenAI` to defer ~240ms import cost
- **Self-registering tools**: Each `tools/*.py` calls `registry.register()` at module level; AST scanning in `discover_builtin_tools()` detects which modules register tools
- **Thread safety**: Registry uses `RLock` + generation counter for snapshot-based reads; SessionDB uses WAL mode with application-level retry + jitter
- **check_fn caching**: Tool availability checks are TTL-cached (30s) to avoid repeat probes of Docker, Modal, Playwright on every definitions call
- **Tool results must be JSON strings**: All tool handlers return `json.dumps(...)`. Use `tool_error()` and `tool_result()` helpers from `tools.registry`
