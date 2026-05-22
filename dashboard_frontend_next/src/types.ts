export type Tone = "success" | "warning" | "danger" | "primary" | "neutral" | string;

export type Metric = {
  label: string;
  value: unknown;
  tone?: Tone;
};

export type Row = Record<string, unknown>;

export type DashboardSnapshot = {
  title: string;
  read_only: boolean;
  display_currency: "KRW" | "USD";
  header: {
    mode: string;
    order_posture: string;
    operator_config?: { fingerprint?: string };
    config_path: string;
    state_path: string;
    audit_path: string;
  };
  operator_home: Row;
  system_verdict: {
    title: string;
    copy: string;
    tone: Tone;
    status_metrics: Metric[];
    capital_summary: Metric[];
    asset_summary_rows: Row[];
    reason_rows: Row[];
  };
  symphony_map: {
    metrics: Metric[];
    nodes: Row[];
    attention_items: Row[];
    verdict_reason_rows: Row[];
    asset_summary_rows: Row[];
    freshness: Row[];
    run_index: Row[];
  };
  operator_cockpit: {
    metrics: Metric[];
    attention_items: Row[];
    freshness: Row[];
    health_checks: Row[];
    operator_summary: Row;
    daily_usage: Row;
    live_order_lifecycle: Row;
    risk_decisions: Row[];
    halt_failure_events: Row[];
    run_index: Row[];
  };
  investment_console: {
    metrics: Metric[];
    broker_summary: Row;
    broker_snapshot: Row;
    reconciliation: Row;
    account_performance: Row[];
    account_performance_currency?: string;
    currency_sleeve_performance: Row[];
    total_portfolio_performance: Row[];
    total_portfolio_performance_krw: Row[];
    total_portfolio_performance_usd: Row[];
    fx_snapshot: Row;
    asset_summary_metrics: Metric[];
    asset_summary_rows: Row[];
    strategy_book_performance: Row[];
    strategy_attribution: Row[];
    strategy_book_snapshots: Row[];
    broker_positions: Row[];
    maestro_exposure: Row[];
    portfolio: Row[];
    portfolio_history: Row[];
    broker_history: Row[];
  };
  virtuoso_apps: {
    metrics: Metric[];
    overview: Row[];
    strategies: Array<{
      strategy_id: string;
      concept: Row[];
      operation: Row[];
      performance: Row[];
      attribution: Row[];
      snapshots: Row[];
      runs: Row[];
      config: Row | null;
      summary: Row;
    }>;
  };
  audit_trail: {
    metrics: Metric[];
    strategy_signal_rows: Row[];
    strategy_run_payloads: Row[];
    orders: Row[];
    approvals: Row[];
    broker_snapshots: Row[];
    live_order_events: Row[];
    fill_reconciliation: Row[];
    system_events: Row[];
    run_index: Row[];
  };
  raw: { status: Row };
};
