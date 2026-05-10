# Virtuoso Apps

Virtuoso apps are external strategy packages. They propose data needs,
candidate instruments, and target allocations through `maestro.sdk`; Maestro
owns DataHub access, universe approval, risk, approval, execution, state, and
audit.

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
