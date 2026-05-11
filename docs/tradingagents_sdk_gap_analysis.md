# TradingAgents to Maestro Virtuoso App: SDK Contract Gap Analysis

## 1. Analysis Basis

This document evaluates how `koomink/TradingAgents` could become a Maestro
Virtuoso app.

Analyzed TradingAgents version:

- Repository: `https://github.com/koomink/TradingAgents`
- Observed HEAD: `7e9e7b83c7fcc18d941300b253c6ed24d985788d`
- Package version: `0.2.4`
- Main package API: `TradingAgentsGraph(...).propagate(ticker, trade_date)`

The core question is not whether TradingAgents can be wrapped at all. It can.
The practical question is whether it can run as a Maestro-compliant Virtuoso app,
where Maestro owns data access, universe validation, risk, approval, execution,
state, monitoring, and audit.

Short answer: a thin paper-mode wrapper is realistic now, but a production-style
Virtuoso app needs SDK and DataHub contract additions.

## 2. TradingAgents Execution Model

TradingAgents analyzes one ticker on one trade date. It runs a LangGraph workflow
with analyst, researcher, trader, risk, and portfolio-manager agents.

```text
Input: ticker="NVDA", trade_date="2026-01-15"
  |
  v
Analyst agents
  - Market analyst: OHLCV and technical indicators
  - Social/sentiment analyst: ticker-specific news and sentiment
  - News analyst: global news and insider transactions
  - Fundamentals analyst: company fundamentals and statements
  |
  v
Researcher debate
  - Bull researcher
  - Bear researcher
  - Research manager
  |
  v
Trader agent
  - TraderProposal: Buy, Hold, or Sell plus rationale
  |
  v
Risk debate and portfolio manager
  - PortfolioDecision: Buy, Overweight, Hold, Underweight, or Sell
  - Executive summary, investment thesis, optional price target and horizon
```

TradingAgents currently fetches data through its own LangChain tools and vendor
router. The important tool surface is:

| TradingAgents tool | Current vendors | Purpose |
| --- | --- | --- |
| `get_stock_data` | yfinance, Alpha Vantage | OHLCV price history |
| `get_indicators` | yfinance plus stockstats, Alpha Vantage | MACD, RSI, Bollinger Bands, moving averages, ATR, VWMA |
| `get_fundamentals` | yfinance, Alpha Vantage | Company profile and fundamental metrics |
| `get_balance_sheet` | yfinance, Alpha Vantage | Balance sheet |
| `get_cashflow` | yfinance, Alpha Vantage | Cash flow statement |
| `get_income_statement` | yfinance, Alpha Vantage | Income statement |
| `get_news` | yfinance, Alpha Vantage | Ticker-specific news |
| `get_global_news` | yfinance, Alpha Vantage | Broad market and macro news |
| `get_insider_transactions` | yfinance, Alpha Vantage | Insider transactions |

Its final output is not a portfolio allocation. It is a structured/prose decision
such as `Buy`, `Overweight`, `Hold`, `Underweight`, or `Sell`, with supporting
reports.

## 3. Current Maestro SDK Contract

Virtuoso apps communicate with Maestro through `maestro.sdk` only.

Current public SDK classes:

| SDK class | Role |
| --- | --- |
| `BaseStrategyPlugin` | Abstract base class for external strategy apps |
| `StrategyManifest` | App identity, capabilities, supported modes, SDK version |
| `StrategyContext` | Run context from Maestro to the app |
| `CandidateInstrumentRequest` | Research or tradable universe candidate proposal |
| `DataRequest` | Data request from app to Maestro DataHub |
| `DataBundle` | DataHub response from Maestro to app |
| `TargetAllocationResult` | Final strategy output accepted by Maestro today |

Every Virtuoso app currently implements:

```python
class BaseStrategyPlugin(ABC):
    def manifest(self) -> StrategyManifest: ...

    def build_candidate_requests(
        self,
        context: StrategyContext,
    ) -> list[CandidateInstrumentRequest]: ...

    def build_data_requests(
        self,
        context: StrategyContext,
    ) -> list[DataRequest]: ...

    def run(
        self,
        data_bundle: DataBundle,
        context: StrategyContext,
    ) -> TargetAllocationResult: ...
```

Current SDK contract version support is `1.0`.

Current `DataRequest` fields are:

- `symbol`
- `asset_type`
- `data_type`
- `intended_use`
- `timeframe`
- `lookback`
- `start`
- `end`
- `as_of`
- `indicator`
- `limit`
- `query`
- `statement_type`
- `frequency`
- `provider_hint`
- `source_hint`
- `fields`

These richer fields support TradingAgents-style data and runtime-tool requests
without requiring the app to import Maestro DataHub internals.

Current `TargetAllocationResult` fields are:

- `strategy_id`
- `strategy_version`
- `timestamp`
- `allocations`
- `allocation_sleeves`
- `confidence`
- `time_horizon`
- `rationale`
- `risk_flags`
- `metadata`

`TargetAllocationResult.metadata` can carry raw TradingAgents reports,
structured ratings, tool traces, model details, or intermediate artifact
references alongside an executable allocation proposal.

The public SDK also defines `StrategySignalResult` for directional LLM research
outputs. The current Maestro execution pipeline still runs only
`target_allocation` plugins end-to-end, so `strategy_signal` plugins need a
signal-to-allocation policy before they can be loaded for execution.

## 4. Expected Maestro to TradingAgents App Contract

A minimal TradingAgents Virtuoso wrapper would look like this:

1. `manifest()`
   - Declare `strategy_id="tradingagents"`.
   - Declare supported asset types such as US stocks and ETFs.
   - Declare data types such as `ohlcv`, `news`, `sentiment`, `fundamental`,
     `technical_indicators`, `financial_statements`, and
     `insider_transactions`.
   - Set `supports_dynamic_universe=True` if tickers are selected dynamically.

2. `build_candidate_requests(context)`
   - Propose the target ticker as `intended_use="tradable"`.
   - Propose reference symbols, benchmarks, macro series, and news topics as
     `intended_use="research"`.

3. `build_data_requests(context)`
   - Request the ticker's price, OHLCV, news, sentiment, fundamentals, financial
     statements, and insider activity.
   - Request benchmark or macro research data separately from tradable data.

4. `run(data_bundle, context)`
   - Convert Maestro-provided data into the format TradingAgents tools expect,
     or run TradingAgents with Maestro-backed runtime tools.
   - Execute `TradingAgentsGraph.propagate(ticker, trade_date)`.
   - Convert the final rating into a Maestro `TargetAllocationResult`.
   - Include TradingAgents' rationale in `rationale`; once the SDK supports it,
     include full structured reports in metadata/artifacts.

Example rating-to-weight mapping for a wrapper:

```python
RATING_TO_WEIGHT = {
    "Buy": 0.30,
    "Overweight": 0.20,
    "Hold": 0.10,
    "Underweight": 0.05,
    "Sell": 0.00,
}
```

This mapping is app policy, not alpha from Maestro. Maestro should still validate
the resulting allocation against its universe, risk, approval, and execution
rules.

## 5. Gap Analysis

### 5.1 What Works With the Current SDK

A fast MVP wrapper is realistic if the wrapper is allowed to let TradingAgents
use its own data vendors and LLM APIs directly.

That MVP can:

- Run in paper mode.
- Analyze one configured ticker per run.
- Use TradingAgents' own yfinance, Alpha Vantage, and LLM provider settings.
- Convert the final rating into a `TargetAllocationResult`.
- Let Maestro handle portfolio combination, risk checks, order proposal, paper
  execution, state, and audit after the allocation is returned.

This is useful for experimentation, but it weakens the current Virtuoso boundary:
apps are supposed to request data through Maestro, not call research vendors
directly.

### 5.2 Data Contract Gaps

| TradingAgents need | Current Maestro status | Gap |
| --- | --- | --- |
| OHLCV prices | Supported through Yahoo/CSV providers | Low |
| Technical indicators | `technical_indicators` provider derives RSI, MACD, SMA, EMA, and Bollinger values from OHLCV | Low |
| Company fundamentals | Yahoo/yfinance provider returns key `Ticker.info` metrics through `fundamental` | Low |
| Financial statements | Yahoo/yfinance provider returns balance sheet, income statement, and cashflow rows through `financial_statements` | Low |
| Ticker-specific news | RSS exists, but filtering and source coverage are weaker than TradingAgents expects | Medium |
| Global news | RSS exists, but no TradingAgents-equivalent tool contract | Medium |
| Insider transactions | Not supported | High |
| Sentiment | Rule-based provider only | Medium |

The deeper issue is not just missing providers. TradingAgents' agents dynamically
call tools while reasoning. Maestro's current SDK asks the strategy to declare
all `DataRequest` objects before `run()`. That can work for predictable data, but
it does not match LangChain tool-calling where the LLM decides which indicators,
news lookbacks, or statement details to request during the graph execution.

### 5.3 Output Contract Gaps

TradingAgents output:

```python
PortfolioDecision(
    rating="Buy",
    executive_summary="...",
    investment_thesis="...",
    price_target=195.0,
    time_horizon="3-6 months",
)
```

Maestro output required today:

```python
TargetAllocationResult(
    strategy_id="tradingagents",
    strategy_version="0.2.4",
    timestamp=...,
    allocations={"AAPL": 0.30, "CASH": 0.70},
    confidence=0.75,
    time_horizon="3-6 months",
    rationale="...",
)
```

This requires a conversion layer. Without a standard signal contract, every
wrapper will invent its own rating-to-allocation policy and lose structured
details from the TradingAgents decision.

### 5.4 Runtime and Operations Gaps

TradingAgents is a long-running LLM workflow. It has checkpoint/resume support,
memory logs, full-state JSON logs, model/provider settings, and optional
callbacks.

The current Maestro SDK has no explicit contract for:

- App-scoped persistent storage paths.
- Checkpoint and resume state.
- Intermediate reports and artifacts.
- Token, cost, latency, and tool-call telemetry.
- App-level timeout or cancellation budget.
- LLM provider/model capability declarations.
- Required environment variables such as LLM provider keys or
  `ALPHA_VANTAGE_API_KEY`.
- Permission boundaries for external network calls.

For live or private-beta use, these should be explicit rather than hidden inside
the app package.

### 5.5 Dynamic Universe Gaps

Maestro already has `CandidateInstrumentRequest`, research/tradable intent, and
a dynamic universe service. That is the right direction for TradingAgents.

The remaining practical gaps are:

- The orchestrator currently evaluates candidates conservatively and does not
  inject real broker tradability and data freshness checkers in the default flow.
- Operator-approved candidate sets are not yet part of a complete app-facing
  approval loop.
- TradingAgents often needs research-only references such as benchmarks, macro
  series, and broad news topics; these must remain separate from tradable
  allocation symbols.

## 6. SDK Additions Required

### 6.1 Runtime Data Tool Contract

Add an SDK-level way for Virtuoso apps to obtain Maestro-backed runtime tools.
For TradingAgents, this should cover:

- `get_stock_data`
- `get_indicators`
- `get_fundamentals`
- `get_balance_sheet`
- `get_cashflow`
- `get_income_statement`
- `get_news`
- `get_global_news`
- `get_insider_transactions`

This contract should let LangChain/LangGraph tools call Maestro DataHub without
the app importing `maestro.datahub` internals.

### 6.2 Structured Signal Result

The SDK now has both pieces of the intended structure: a public
`StrategySignalResult` type and `TargetAllocationResult.metadata` for wrappers
that still return executable allocations.

Current signal shape:

```python
class StrategySignalResult(BaseModel):
    strategy_id: str
    strategy_version: str
    timestamp: datetime
    symbol: str
    action: Literal["buy", "hold", "sell"]
    rating: str | None = None
    confidence: float
    price_target: float | None = None
    stop_loss: float | None = None
    time_horizon: str | None = None
    position_sizing: str | None = None
    rationale: str | None = None
    risk_flags: list[str] = []
    metadata: dict[str, Any] = {}
```

The remaining work is to add a Maestro-owned signal-to-allocation policy that is
explicit, testable, and auditable. Until then, TradingAgents wrappers should
return `TargetAllocationResult` and include `rating`, `action`, `price_target`,
`reports`, `model_provider`, and `tool_trace` in `metadata`.

### 6.3 Richer DataRequest

`DataRequest` has been extended so app data needs and runtime tools can be
represented without ad hoc fields:

- `start`
- `end`
- `as_of`
- `indicator`
- `limit`
- `query`
- `statement_type`
- `frequency`
- `provider_hint` and `source_hint`, if Maestro wants controlled provider
  preference without giving apps direct provider access

### 6.4 DataHub Provider Expansion

Provider/data type implementation status:

- `technical_indicators`
- `fundamental`
- `financial_statements`
- `insider_transactions`

The first three are implemented through the Yahoo/yfinance provider family.
`insider_transactions` remains recognized by the SDK/DataHub type registry but
does not yet have a provider implementation. Alpha Vantage parity can come
later.

### 6.5 App Runtime Services

Expose SDK/runtime support for:

- App-scoped storage directories.
- Checkpoint files.
- Artifact logging.
- Intermediate event emission.
- Timeout and cancellation budgets.
- Token/cost/latency callbacks.
- App health/status summaries.

TradingAgents' `results_dir`, `data_cache_dir`, and `memory_log_path` should be
mapped to Maestro-controlled app storage rather than defaulting to hidden global
paths.

### 6.6 LLM Capability and Permission Contract

Extend `StrategyManifest` or app config with:

- `requires_llm`
- Supported provider/model names.
- Required environment variables.
- Expected network domains or permission categories.
- Estimated runtime.
- Whether direct external data vendor calls are allowed.

This keeps TradingAgents' LLM-heavy behavior visible to operators before a run.

### 6.7 Dynamic Universe Completion

Finish the operational loop around dynamic candidates:

- Connect broker tradability checks.
- Connect DataHub freshness checks.
- Carry operator-approved symbols into candidate evaluation.
- Persist temporary versus persistent approvals clearly.
- Keep research-only references impossible to allocate.

## 7. Integration Options

### Option A: Fast Paper Wrapper

Keep TradingAgents mostly intact and let it call its own data vendors and LLM
providers.

Pros:

- Fastest path to a demo.
- Minimal Maestro SDK changes.
- Good for validating whether the strategy output is useful.

Cons:

- Not fully Maestro-compliant.
- Data access, cost, telemetry, and vendor failures are hidden inside the app.
- Weaker audit trail.

Use this only for paper experimentation.

### Option B: Maestro-Backed Tool Adapter

Replace TradingAgents data tools with Maestro DataHub-backed tools while keeping
the TradingAgents graph and agents mostly intact.

Pros:

- Preserves Maestro's ownership of data access and audit.
- Keeps TradingAgents' main architecture.
- Makes provider failures and freshness checks visible to Maestro.

Cons:

- Requires SDK runtime tool support.
- Requires richer data types and provider expansion.
- Requires careful test fixtures for LLM/tool interactions.

This is the recommended direction for a real Virtuoso app.

### Option C: Deeper TradingAgents Fork

Refactor TradingAgents so it natively speaks Maestro SDK contracts instead of
using its original tool/data abstractions.

Pros:

- Cleanest long-term contract.
- Less adapter complexity.

Cons:

- Highest maintenance burden.
- More divergence from upstream TradingAgents.

Use this only if TradingAgents becomes a strategic first-class app.

## 8. Recommended Work Order

1. Build a paper-only wrapper proof of concept with an explicit
   rating-to-allocation policy.
2. Add a structured signal/metadata contract so TradingAgents decisions are not
   collapsed into weights too early.
3. Add Maestro-backed runtime data tools for the TradingAgents tool surface.
4. Add an `insider_transactions` provider and broaden Yahoo/Alpha Vantage parity
   where needed.
5. Add app runtime services for storage, artifacts, checkpointing, timeout, and
   telemetry.
6. Add LLM capability and permission declarations to the app manifest/config.
7. Complete dynamic universe checks for broker tradability, data freshness, and
   operator approval.
8. Promote from paper to live-approval only after audit, replay, and approval
   flows include the TradingAgents reports and signal conversion policy.

## 9. Conclusion

The current Maestro SDK can host a thin TradingAgents wrapper for paper-mode
experiments, but it is not yet sufficient for a production-style Virtuoso app.

The biggest blocker is not merely that a few DataHub providers are missing. The
larger contract mismatch is that TradingAgents is an interactive, LLM tool-calling
research graph that produces structured directional decisions, while Maestro
currently expects predeclared data requests and final target allocations.

The right integration path is:

1. Keep TradingAgents proposal-only.
2. Keep Maestro responsible for data access, universe validation, risk, approval,
   execution, state, monitoring, and audit.
3. Add SDK support for runtime DataHub-backed tools, structured signal results,
   richer data requests, app runtime services, and explicit LLM permissions.

With those additions, TradingAgents can become a realistic Virtuoso app without
breaking Maestro's core ownership boundaries.
