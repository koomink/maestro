# Virtuoso Apps

Virtuoso apps are external strategy packages. They propose data needs,
candidate instruments, strategy signals, and target allocations through
`maestro.sdk`; Maestro owns DataHub access, universe approval, risk, approval,
execution, state, and audit.

## SDK Boundary

- Import only from `maestro.sdk`.
- Do not import Maestro broker, execution, state, orchestration, approval,
  Telegram, KIS, or DataHub internals.
- Do not call external research APIs directly; request data through
  `DataRequest`.
- Do not call broker APIs directly; propose `CandidateInstrumentRequest` entries
  when a strategy needs research or tradable candidates.
- Keep `StrategyManifest.sdk_contract_version` at or below the Maestro-supported
  SDK contract version.
- Set `StrategyManifest.requires_llm`, `supported_llm_providers`,
  `required_env_vars`, `estimated_runtime_seconds`, and
  `allow_direct_external_data_calls` when an app has LLM or external network
  requirements.

## SDK Contract 1.0

SDK contract `1.0` keeps `TargetAllocationResult` as the only result type the
current Maestro execution pipeline can run end-to-end. The public SDK also
defines `StrategySignalResult` for LLM research apps, but plugins declaring
`StrategyManifest.result_type = "strategy_signal"` are rejected at load time
until Maestro has a signal-to-allocation policy in the execution pipeline.

`DataRequest` supports predeclared data needs and richer runtime-tool shapes:
`start`, `end`, `as_of`, `indicator`, `limit`, `query`, `statement_type`,
`frequency`, `provider_hint`, `source_hint`, and `fields`. These fields let apps
describe TradingAgents-style requests without importing DataHub internals.

`TargetAllocationResult.metadata` is available for structured source signals,
ratings, report summaries, tool traces, and model details that should travel
with the final allocation proposal.

## Dynamic Universe

`CandidateInstrumentRequest.intended_use` must be either `research` or
`tradable`.

Research candidates remain outside the tradable universe and cannot receive
allocation weight. Tradable candidates must pass Maestro `UniversePolicy`,
operator approval, broker tradability checks, and DataHub freshness checks before
they can become temporary or persistent tradable entries.

The current conservative default policy allows only US stock/ETF candidates for
KIS overseas stock trading on NASD, NYSE, or AMEX. It allows at most one new
tradable candidate per run and requires operator approval.

## Packaging

An app package should expose one `BaseStrategyPlugin` implementation through a
stable `module:ClassName` entrypoint. Keep app dependencies explicit in the app
package and avoid requiring Maestro to import optional provider SDKs unless they
are part of the app itself.

Static `portfolio.allowed_symbols` configs remain valid for examples,
tutorials, tests, and conservative paper runs.
