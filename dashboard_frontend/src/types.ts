export type Tone = "success" | "warning" | "danger" | "primary" | "neutral";

export type Metric = {
  label: string;
  value: unknown;
  tone?: Tone;
};

export type Row = Record<string, unknown>;

export type SignalFreshness = {
  overall: "missing" | "fresh" | "stale" | "failed" | string;
  strategies: Array<{
    strategy_id: string;
    status: "missing" | "fresh" | "stale" | "failed" | string;
    latest_signal_run_id?: string | null;
    latest_signal_at?: string | null;
    age_seconds?: number | null;
    max_age_seconds?: number | null;
  }>;
};


export type PipelineNode = {
  id: string;
  label: string;
  status: unknown;
  tone: Tone;
  detail: unknown;
  updated_at?: string | null;
  updated_at_display?: string | null;
  run_id?: string | null;
  next_check?: string | null;
};

export type WorkflowPipelineApp = {
  strategy_id: string;
  display_name: string;
  account_id?: string | null;
  account_ids?: string[];
  account_currency?: string | null;
  nodes: PipelineNode[];
};

export type DataSourceNode = {
  id: string;
  provider: string;
  data_types: string[];
  priority?: number | null;
  status: string;
  tone: Tone;
  issue_count: number;
  last_used_at?: string | null;
  last_used_at_display?: string | null;
  strategy_ids: string[];
};

export type WorkflowPipelines = {
  data_sources?: DataSourceNode[];
  system: { nodes: PipelineNode[] };
  apps: WorkflowPipelineApp[];
};

export type DashboardRefreshResponse = {
  status: string;
  accounts_synced: number;
  signal_freshness: SignalFreshness;
};

export type GenerateSignalResponse = {
  status: string;
  strategy_id: string;
  signal_run_id?: string | null;
  loaded_strategies: string[];
  action_required: boolean;
  orders_preview_count: number;
};

export type RunDetail = {
  run_id: string;
  summary: Record<string, number>;
  timeline: Row[];
  strategy_runs: Row[];
  orders: Row[];
  approvals: Row[];
  risk_decisions: Row[];
  system_events: Row[];
  broker_snapshots: Row[];
  portfolio_snapshots: Row[];
  strategy_book_snapshots: Row[];
};

export type AppPerformanceSnapshot = {
  schema_version: number;
  latest: Row;
  series: {
    value: Row[];
    cash_flow_markers: Row[];
  };
  quality: { status: "ok" | "missing_cash_flow_events" | "unattributed_account_flow" | "insufficient_history" | string; reasons: Row[] };
  lineage: Row;
};

export type BookGate = {
  label: string;
  detail: string;
  status: "pass" | "fail" | "info" | string;
};

export type StrategyBookPerformance = {
  dates: string[];
  equity: number[];
  metrics: Row;
  window_start?: string | null;
  exec_map?: Record<string, string>;
  generated_at?: string | null;
  data_through?: string | null;
  note?: string | null;
};

export type StrategyBook = {
  book_id: string;
  label: string;
  created_at?: string | null;
  run_id?: string | null;
  target_weight?: number | null;
  allocations: Record<string, number>;
  rationale?: string | null;
  state?: "risk_on" | "risk_off" | "cash" | string | null;
  universe: Record<string, string[]>;
  signal_evidence: BookGate[];
  signal_allocations: Record<string, number>;
  execution_map: Record<string, string>;
  performance: StrategyBookPerformance | null;
};

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
    broker_account_overview: { summary: Row; metrics: Metric[]; accounts: Row[] };
    broker_snapshot: Row;
    reconciliation: Row;
    account_performance: Row[];
    account_performance_currency?: string;
    currency_sleeve_performance: Row[];
    total_portfolio_performance: Row[];
    total_portfolio_performance_krw: Row[];
    total_portfolio_performance_usd: Row[];
    performance_snapshot: {
      schema_version: number;
      display_currency: "KRW" | "USD";
      latest: Row;
      series: {
        account: Row[];
        currency_sleeves: Row[];
        total_portfolio: Row[];
        total_portfolio_krw: Row[];
        total_portfolio_usd: Row[];
        strategy_books: Row[];
        strategy_attribution: Row[];
      };
      quality: { status: "ok" | "warning" | "missing" | string; reasons: Row[] };
      fx: Row;
      lineage: Row;
    };
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
  workflow_pipelines: WorkflowPipelines;
  virtuoso_apps: {
    metrics: Metric[];
    overview: Row[];
    signal_freshness: SignalFreshness;
    strategies: Array<{
      strategy_id: string;
      concept: Row[];
      operation: Row[];
      performance: Row[];
      target_performance: Row[];
      performance_snapshot: AppPerformanceSnapshot;
      attribution: Row[];
      snapshots: Row[];
      books: StrategyBook[];
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
