import { useMemo, useRef, useState } from "react";
import type React from "react";
import type { DashboardSnapshot, PipelineNode, Row, Tone } from "../types";
import { dateTime } from "../utils/data";
import { useNow, usePrefersReducedMotion } from "../utils/hooks";
import { toneFromValue } from "../utils/tone";

// ═══════════════════════════════════════════════════════════════
// PIPELINE DASHBOARD
// Live topology of the Maestro system:
//   DATA SOURCES → VIRTUOSO APPS → ACCOUNTS → OPERATING GATE
// Hovering a Virtuoso app lights its full pipeline in blue with a
// relay pulse travelling node-to-node; everything downstream of a
// broken stage turns red.
// ═══════════════════════════════════════════════════════════════

// ── Geometry ──
const VB_W = 1100;
const VB_H = 560;
const BAND_TOP = 52;
const BAND_BOTTOM = 486;
const BAND_H = BAND_BOTTOM - BAND_TOP;
const RAIL_Y = 508;

const COLS = {
  data: { x: 24, w: 192 },
  app: { x: 296, w: 232 },
  acct: { x: 608, w: 200 },
  gate: { x: 888, w: 188 },
} as const;

const FLOW_COLOR = "#58b2ff";
const CUT_COLOR = "var(--red)";

type Box = { x: number; y: number; w: number; h: number };

function toneColor(tone: Tone | string | undefined): string {
  switch (tone) {
    case "success":
      return "var(--green)";
    case "warning":
      return "var(--amber)";
    case "danger":
      return "var(--red)";
    case "primary":
      return "var(--cyan)";
    default:
      return "rgba(154, 166, 186, 0.7)";
  }
}

function relTime(dateStr: unknown, nowMs: number): string {
  const t = dateTime(dateStr);
  if (!Number.isFinite(t)) return "—";
  const s = Math.floor((nowMs - t) / 1000);
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

/** Vertically center `count` uniform slots inside the content band. */
function slotLayout(count: number, preferredH: number, gap: number) {
  const h = Math.min(preferredH, Math.floor((BAND_H - (count - 1) * gap) / Math.max(count, 1)));
  const total = count * h + (count - 1) * gap;
  const start = BAND_TOP + Math.max(0, (BAND_H - total) / 2);
  return { h, y: (index: number) => start + index * (h + gap) };
}

/** Horizontal cubic edge between two anchor points. */
function hEdge(x1: number, y1: number, x2: number, y2: number): string {
  const dx = Math.max(36, (x2 - x1) * 0.55);
  return `M${x1} ${y1} C${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

/** Vertical cubic edge (gate-internal chain). */
function vEdge(x1: number, y1: number, x2: number, y2: number): string {
  const dy = Math.max(14, (y2 - y1) * 0.5);
  return `M${x1} ${y1} C${x1} ${y1 + dy}, ${x2} ${y2 - dy}, ${x2} ${y2}`;
}

// ── Pipeline semantics ──
// Node stages along an app's pipeline. Edges of stage k connect
// stage-k nodes to stage-(k+1) nodes.
const STAGE_COUNT = 5; // data→app, app→acct, acct→risk, risk→exec, exec→state

const BROKEN_STATUSES = new Set([
  "missing",
  "failed",
  "fail",
  "blocked",
  "rejected",
  "halted",
  "error",
]);

function isBroken(node: { status?: unknown; tone?: unknown } | undefined): boolean {
  if (!node) return true;
  if (String(node.tone) === "danger") return true;
  return BROKEN_STATUSES.has(String(node.status ?? "").toLowerCase());
}

// ── View models ──

type AppVM = {
  id: string;
  name: string;
  currency: string | null;
  accountIds: string[];
  nodes: Map<string, PipelineNode>;
  enabled: boolean;
  signalStatus: string;
  signalTone: Tone;
  signalAt: unknown;
  retLabel: string | null;
  retTone: Tone;
  perf: number[];
};

type AcctVM = {
  id: string;
  row: Row | null;
  appIds: string[];
};

function buildAppVMs(snapshot: DashboardSnapshot): AppVM[] {
  const freshness = snapshot.virtuoso_apps.signal_freshness.strategies;
  return snapshot.workflow_pipelines.apps.map((app) => {
    const nodes = new Map(app.nodes.map((node) => [node.id, node]));
    const strategy = snapshot.virtuoso_apps.strategies.find(
      (s) => s.strategy_id === app.strategy_id,
    );
    const signal = freshness.find((f) => f.strategy_id === app.strategy_id);
    const signalNode = nodes.get("signal");
    const signalStatus = String(signal?.status || signalNode?.status || "missing");
    const perf = (strategy?.performance || [])
      .map((p) => Number(p.current_value))
      .filter(Number.isFinite)
      .reverse();
    const ret = strategy?.summary.cumulative_return;
    const retNum = ret == null ? Number.NaN : Number(ret);
    return {
      id: app.strategy_id,
      name: app.display_name,
      currency: app.account_currency ?? null,
      accountIds: app.account_ids?.length
        ? app.account_ids
        : app.account_id
          ? [String(app.account_id)]
          : [],
      nodes,
      enabled: String(nodes.get("app")?.status) === "enabled",
      signalStatus,
      signalTone:
        signalStatus === "fresh"
          ? "success"
          : signalStatus === "failed"
            ? "danger"
            : signalStatus === "passed"
              ? "success"
              : "warning",
      signalAt: signal?.latest_signal_at ?? signalNode?.updated_at,
      retLabel: Number.isFinite(retNum)
        ? `${retNum >= 0 ? "+" : ""}${(retNum * 100).toFixed(1)}%`
        : null,
      retTone: !Number.isFinite(retNum) ? "neutral" : retNum >= 0 ? "success" : "danger",
      perf,
    };
  });
}

function buildAcctVMs(snapshot: DashboardSnapshot, apps: AppVM[]): AcctVM[] {
  const overview = snapshot.investment_console.broker_account_overview.accounts;
  const rowById = new Map(overview.map((row) => [String(row.account_id), row]));
  const ordered: AcctVM[] = [];
  const seen = new Set<string>();
  // Accounts appear in app order first so edges stay short, then any
  // overview accounts not owned by a visible app.
  for (const app of apps) {
    for (const id of app.accountIds) {
      if (seen.has(id)) continue;
      seen.add(id);
      ordered.push({ id, row: rowById.get(id) ?? null, appIds: [] });
    }
  }
  for (const row of overview) {
    const id = String(row.account_id);
    if (seen.has(id)) continue;
    seen.add(id);
    ordered.push({ id, row, appIds: [] });
  }
  for (const acct of ordered) {
    acct.appIds = apps.filter((app) => app.accountIds.includes(acct.id)).map((app) => app.id);
  }
  return ordered;
}

function findCheck(checks: Row[], needle: string): Row | undefined {
  return checks.find((row) => String(row.check ?? "").toLowerCase().includes(needle));
}

/** "yahoo_market" → "Yahoo Market", keeping known acronyms uppercase. */
function sourceTitle(id: string): string {
  return id
    .split(/[_\s-]+/)
    .map((word) =>
      ["fred", "kis", "gdelt", "rss", "csv", "api"].includes(word.toLowerCase())
        ? word.toUpperCase()
        : word.charAt(0).toUpperCase() + word.slice(1),
    )
    .join(" ");
}

function dataTypesLabel(types: string[]): string {
  if (!types.length) return "all data types";
  if (types.length <= 2) return types.join("/");
  return `${types[0]}/${types[1]} +${types.length - 2}`;
}

// ── Relay pulse ──
// A light pulse that travels its edge during stage-slot `stage` of a
// shared cycle, so pulses appear to hand off node-to-node.
function RelayPulse({
  path,
  stage,
  color,
  durPerStage = 0.8,
}: {
  path: string;
  stage: number;
  color: string;
  durPerStage?: number;
}) {
  const cycle = STAGE_COUNT * durPerStage + 0.6; // brief rest between sweeps
  const t0 = (stage * durPerStage) / cycle;
  const t1 = ((stage + 1) * durPerStage) / cycle;
  const points: string[] = [];
  const times: string[] = [];
  if (t0 > 0) {
    points.push("0");
    times.push("0");
  }
  points.push("0", "1");
  times.push(t0.toFixed(4), t1.toFixed(4));
  if (t1 < 1) {
    points.push("1");
    times.push("1");
  }
  const eps = 0.008;
  const opacityTimes = [
    "0",
    Math.max(t0, 0.0001).toFixed(4),
    Math.min(t0 + eps, 1).toFixed(4),
    Math.max(t1 - eps, 0).toFixed(4),
    Math.min(t1, 0.9999).toFixed(4),
    "1",
  ].join(";");
  return (
    <g className="pd-pulse">
      <circle r={10} fill={color} opacity={0} filter="url(#pdPulseGlow)">
        <animateMotion
          calcMode="linear"
          dur={`${cycle}s`}
          keyPoints={points.join(";")}
          keyTimes={times.join(";")}
          path={path}
          repeatCount="indefinite"
        />
        <animate
          attributeName="opacity"
          dur={`${cycle}s`}
          keyTimes={opacityTimes}
          repeatCount="indefinite"
          values="0;0;0.28;0.28;0;0"
        />
      </circle>
      <circle r={4} fill={color} opacity={0}>
        <animateMotion
          calcMode="linear"
          dur={`${cycle}s`}
          keyPoints={points.join(";")}
          keyTimes={times.join(";")}
          path={path}
          repeatCount="indefinite"
        />
        <animate
          attributeName="opacity"
          dur={`${cycle}s`}
          keyTimes={opacityTimes}
          repeatCount="indefinite"
          values="0;0;0.95;0.95;0;0"
        />
      </circle>
    </g>
  );
}

function Sparkline({ data, x, y, w, h, tone }: { data: number[]; x: number; y: number; w: number; h: number; tone: Tone }) {
  if (data.length < 2) return null;
  const min = Math.min(...data);
  const max = Math.max(...data);
  const range = max - min || 1;
  const pts = data.map((v, i) => `${x + (i / (data.length - 1)) * w},${y + h - ((v - min) / range) * (h - 2)}`);
  const color = tone === "danger" ? "var(--red)" : tone === "success" ? "var(--green)" : "var(--cyan)";
  return (
    <g className="pd-spark">
      <polygon fill={color} opacity={0.07} points={`${pts.join(" ")} ${x + w},${y + h} ${x},${y + h}`} />
      <polyline fill="none" points={pts.join(" ")} stroke={color} strokeLinecap="round" strokeLinejoin="round" strokeWidth={1.4} opacity={0.85} />
    </g>
  );
}

function StatusDot({ cx, cy, tone, blink = false }: { cx: number; cy: number; tone: Tone | string; blink?: boolean }) {
  return (
    <g>
      <circle cx={cx} cy={cy} fill={toneColor(tone)} opacity={0.22} r={5.5} />
      <circle cx={cx} cy={cy} fill={toneColor(tone)} r={3}>
        {blink && <animate attributeName="opacity" dur="1.4s" repeatCount="indefinite" values="1;0.35;1" />}
      </circle>
    </g>
  );
}

// ═══ Main component ═══

export function PipelineDashboard({
  openApp,
  snapshot,
}: {
  openApp: (strategyId: string) => void;
  snapshot: DashboardSnapshot;
}) {
  const [hoverId, setHoverId] = useState<string | null>(null);
  const [tooltip, setTooltip] = useState<{ x: number; y: number; title: string; rows: Array<[string, string, Tone]> } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const reducedMotion = usePrefersReducedMotion();
  const now = useNow(30000).getTime();

  const apps = useMemo(() => buildAppVMs(snapshot), [snapshot]);
  const accts = useMemo(() => buildAcctVMs(snapshot, apps), [snapshot, apps]);

  const checks = snapshot.operator_cockpit.health_checks;
  const marketCheck = findCheck(checks, "datahub") || findCheck(checks, "market");
  const brokerCheck = findCheck(checks, "broker_snapshot");
  const summary = snapshot.investment_console.broker_account_overview.summary;
  const freshAccounts = Number(summary.fresh_accounts);
  const configuredAccounts = Number(summary.configured_accounts);
  const brokerCountsKnown = Number.isFinite(freshAccounts) && Number.isFinite(configuredAccounts);

  const marketTone: Tone = marketCheck ? toneFromValue(marketCheck.status) : "neutral";
  const brokerTone: Tone =
    brokerCountsKnown && freshAccounts === configuredAccounts && configuredAccounts > 0
      ? "success"
      : brokerCheck
        ? toneFromValue(brokerCheck.message ?? brokerCheck.status)
        : "warning";

  const systemNodes = new Map(
    snapshot.workflow_pipelines.system.nodes.map((node) => [node.id, node]),
  );
  const riskNode = systemNodes.get("risk");
  const outputNode = systemNodes.get("output");
  const stateNode = systemNodes.get("state");
  const orderPosture = String(snapshot.header.order_posture || "unknown");
  const mode = String(snapshot.header.mode || "unknown");
  const publishedState = String(snapshot.operator_home.status ?? "unknown");

  // ── Data sources (one node per configured DataHub provider + broker sync) ──
  const visibleApps = apps.slice(0, 4);
  const visibleAccts = accts.slice(0, 5);
  const visibleAppIds = new Set(visibleApps.map((app) => app.id));
  const enabledAppIds = visibleApps.filter((app) => app.enabled).map((app) => app.id);

  type SourceVM = {
    id: string;
    kind: "provider" | "broker";
    title: string;
    meta: string;
    tone: Tone;
    statusText: string;
    appIds: string[];
    broken: boolean;
    rows: Array<[string, string, Tone]>;
  };
  const dataSources = snapshot.workflow_pipelines.data_sources ?? [];
  const providerSources: SourceVM[] = dataSources.slice(0, 3).map((source) => {
    const linked = source.strategy_ids?.filter((id) => visibleAppIds.has(id)) ?? [];
    // Before the first signal run there is no usage evidence yet; assume every
    // enabled app may consume the provider rather than orphaning it.
    const appIds = linked.length ? linked : enabledAppIds;
    return {
      id: source.id,
      kind: "provider",
      title: sourceTitle(source.id),
      meta: `${source.provider} · ${dataTypesLabel(source.data_types)}`,
      tone: source.tone,
      statusText: `${source.status}${source.last_used_at ? ` · used ${relTime(source.last_used_at, now)}` : ""}`,
      appIds,
      broken: isBroken({ status: source.status, tone: source.tone }),
      rows: [
        ["Status", source.status, source.tone],
        ["Data types", source.data_types.join(", ") || "all", "neutral"],
        ["Used by", linked.join(", ") || "no evidence yet", "neutral"],
        ["Last used", relTime(source.last_used_at, now), "neutral"],
        ["Issues", String(source.issue_count), source.issue_count ? "warning" : "neutral"],
      ],
    };
  });
  if (providerSources.length === 0) {
    // Fallback for older backends without data_sources in the snapshot.
    providerSources.push({
      id: "market",
      kind: "provider",
      title: "Market Data",
      meta: marketCheck ? `DataHub · ${String((marketCheck.details as Row | undefined)?.provider ?? "provider")}` : "DataHub",
      tone: marketTone,
      statusText: marketCheck ? `${String(marketCheck.status)} · ${String(marketCheck.message ?? "")}` : "no health check",
      appIds: enabledAppIds,
      broken: isBroken({ status: marketCheck?.status, tone: marketTone }),
      rows: [
        ["Status", String(marketCheck?.status ?? "unknown"), marketTone],
        ["Message", String(marketCheck?.message ?? "n/a"), "neutral"],
      ],
    });
  }
  const brokerSource: SourceVM = {
    id: "broker",
    kind: "broker",
    title: "Broker Sync",
    meta: "KIS REST bridge",
    tone: brokerTone,
    statusText: brokerCountsKnown
      ? `${freshAccounts}/${configuredAccounts} fresh · ${relTime(summary.latest_sync_at, now)}`
      : String(brokerCheck?.message ?? "sync status unknown"),
    appIds: visibleApps.map((app) => app.id),
    broken: false,
    rows: [
      ["Accounts fresh", brokerCountsKnown ? `${freshAccounts}/${configuredAccounts}` : "n/a", brokerTone],
      ["Last sync", relTime(summary.latest_sync_at, now), "neutral"],
      ["Snapshot", String(brokerCheck?.message ?? "n/a"), brokerCheck ? toneFromValue(brokerCheck.message) : "neutral"],
    ],
  };
  const allSources = [...providerSources, brokerSource];

  // ── Layout ──
  const srcSlots = slotLayout(allSources.length, 88, allSources.length > 3 ? 22 : 40);
  const appSlots = slotLayout(visibleApps.length, 118, 28);
  const acctSlots = slotLayout(visibleAccts.length, 84, 20);
  const gateSlots = slotLayout(3, 106, 42);

  const srcPos = new Map<string, Box>(
    allSources.map((source, i) => [source.id, { x: COLS.data.x, y: srcSlots.y(i), w: COLS.data.w, h: srcSlots.h }]),
  );
  const appPos = new Map<string, Box>(
    visibleApps.map((app, i) => [app.id, { x: COLS.app.x, y: appSlots.y(i), w: COLS.app.w, h: appSlots.h }]),
  );
  const acctPos = new Map<string, Box>(
    visibleAccts.map((acct, i) => [acct.id, { x: COLS.acct.x, y: acctSlots.y(i), w: COLS.acct.w, h: acctSlots.h }]),
  );
  const gatePos: Record<"risk" | "exec" | "state", Box> = {
    risk: { x: COLS.gate.x, y: gateSlots.y(0), w: COLS.gate.w, h: gateSlots.h },
    exec: { x: COLS.gate.x, y: gateSlots.y(1), w: COLS.gate.w, h: gateSlots.h },
    state: { x: COLS.gate.x, y: gateSlots.y(2), w: COLS.gate.w, h: gateSlots.h },
  };

  // ── Per-app break analysis ──
  // Stage of the first broken node on an app's path (Infinity = clean).
  function acctBroken(acct: AcctVM | undefined): boolean {
    if (!acct) return true;
    if (!acct.row) return true;
    return String(acct.row.tone) === "danger";
  }
  const acctById = new Map(accts.map((acct) => [acct.id, acct]));
  function appBreakStage(app: AppVM): number {
    if (!app.enabled) return Number.POSITIVE_INFINITY; // suspended, not broken
    const linkedProviders = providerSources.filter((source) => source.appIds.includes(app.id));
    if (linkedProviders.some((source) => source.broken) || isBroken(app.nodes.get("data"))) return 0;
    if (isBroken(app.nodes.get("signal"))) return 1;
    const linked = app.accountIds.map((id) => acctById.get(id));
    if (linked.length > 0 && linked.every((acct) => acctBroken(acct))) return 2;
    const risk = app.nodes.get("risk");
    if (risk && String(risk.status) !== "not_run" && isBroken(risk)) return 3;
    const output = app.nodes.get("output");
    if (output && String(output.tone) === "danger") return 4;
    if (stateNode && String(stateNode.tone) === "danger") return 5;
    return Number.POSITIVE_INFINITY;
  }
  const breakStageByApp = new Map(visibleApps.map((app) => [app.id, appBreakStage(app)]));

  // ── Edges ──
  type EdgeVM = {
    key: string;
    d: string;
    stage: number;
    appId?: string;
    acctId?: string;
    tone: Tone;
  };
  const edges: EdgeVM[] = [];
  const appsBySource = new Map(
    allSources.map((source) => [source.id, visibleApps.filter((app) => source.appIds.includes(app.id))]),
  );
  visibleApps.forEach((app) => {
    const target = appPos.get(app.id);
    if (!target) return;
    const incoming = allSources.filter((source) => source.appIds.includes(app.id));
    incoming.forEach((source, si) => {
      const srcBox = srcPos.get(source.id);
      if (!srcBox) return;
      const linkedApps = appsBySource.get(source.id) ?? [];
      const srcIdx = Math.max(0, linkedApps.findIndex((a) => a.id === app.id));
      const srcY = srcBox.y + srcBox.h / 2 + (srcIdx - (linkedApps.length - 1) / 2) * 9;
      const frac = incoming.length === 1 ? 0.5 : 0.3 + 0.4 * (si / (incoming.length - 1));
      edges.push({
        key: `src-${source.id}-${app.id}`,
        d: hEdge(srcBox.x + srcBox.w, srcY, target.x, target.y + target.h * frac),
        stage: 0,
        appId: app.id,
        tone: !app.enabled
          ? "neutral"
          : source.kind === "broker"
            ? ((app.nodes.get("data")?.tone as Tone) || "neutral")
            : source.tone,
      });
    });
    app.accountIds.forEach((acctId, branch) => {
      const acct = acctPos.get(acctId);
      if (!acct) return;
      const spreadY = app.accountIds.length > 1 ? (branch - (app.accountIds.length - 1) / 2) * 22 : 0;
      edges.push({
        key: `app-${app.id}-${acctId}`,
        d: hEdge(target.x + target.w, target.y + target.h / 2 + spreadY, acct.x, acct.y + acct.h / 2),
        stage: 1,
        appId: app.id,
        acctId,
        tone: app.enabled ? app.signalTone : "neutral",
      });
    });
  });
  visibleAccts.forEach((acct, i) => {
    const from = acctPos.get(acct.id);
    if (!from) return;
    const n = Math.max(visibleAccts.length - 1, 1);
    const targetY = gatePos.risk.y + gatePos.risk.h * (0.22 + 0.56 * (i / n));
    edges.push({
      key: `acct-${acct.id}-risk`,
      d: hEdge(from.x + from.w, from.y + from.h / 2, gatePos.risk.x, targetY),
      stage: 2,
      acctId: acct.id,
      tone: acct.row ? ((acct.row.tone as Tone) || "neutral") : "warning",
    });
  });
  edges.push({
    key: "risk-exec",
    d: vEdge(
      gatePos.risk.x + gatePos.risk.w / 2,
      gatePos.risk.y + gatePos.risk.h,
      gatePos.exec.x + gatePos.exec.w / 2,
      gatePos.exec.y,
    ),
    stage: 3,
    tone: (riskNode?.tone as Tone) || "neutral",
  });
  edges.push({
    key: "exec-state",
    d: vEdge(
      gatePos.exec.x + gatePos.exec.w / 2,
      gatePos.exec.y + gatePos.exec.h,
      gatePos.state.x + gatePos.state.w / 2,
      gatePos.state.y,
    ),
    stage: 4,
    tone: (stateNode?.tone as Tone) || "neutral",
  });

  // ── Hover path state ──
  const hoveredApp = hoverId?.startsWith("app:") ? hoverId.slice(4) : null;
  const hoveredAppVM = hoveredApp ? visibleApps.find((app) => app.id === hoveredApp) : null;
  const breakStage = hoveredApp ? (breakStageByApp.get(hoveredApp) ?? Number.POSITIVE_INFINITY) : Number.POSITIVE_INFINITY;
  const pathSuspended = hoveredAppVM ? !hoveredAppVM.enabled : false;

  function edgeOnPath(edge: EdgeVM): boolean {
    if (!hoveredAppVM) return false;
    if (edge.stage <= 1) return edge.appId === hoveredAppVM.id;
    if (edge.stage === 2) return edge.acctId != null && hoveredAppVM.accountIds.includes(edge.acctId);
    return true; // gate-internal edges are shared by every path
  }
  function edgeState(edge: EdgeVM): "lit" | "cut" | "dim" | "ambient" {
    if (!hoveredAppVM) return "ambient";
    if (!edgeOnPath(edge)) return "dim";
    if (pathSuspended) return "dim";
    if (edge.stage >= breakStage) return "cut";
    if (edge.stage === 2 && edge.acctId && acctBroken(acctById.get(edge.acctId))) return "cut";
    return "lit";
  }

  type NodeRef = {
    stage: number;
    acctId?: string;
    appId?: string;
    srcAppIds?: string[];
    selfBroken?: boolean;
  };
  function nodeState(ref: NodeRef): "lit" | "broken" | "dim" | "ambient" {
    if (!hoveredAppVM) return "ambient";
    const onPath =
      ref.stage >= 3 ||
      (ref.stage === 0 && (ref.srcAppIds?.includes(hoveredAppVM.id) ?? true)) ||
      (ref.stage === 1 && ref.appId === hoveredAppVM.id) ||
      (ref.stage === 2 && ref.acctId != null && hoveredAppVM.accountIds.includes(ref.acctId));
    if (!onPath) return "dim";
    if (pathSuspended) return ref.stage === 1 ? "lit" : "dim";
    if (ref.stage === 2 && ref.acctId && acctBroken(acctById.get(ref.acctId)) && breakStage >= 2) return "broken";
    if (ref.stage > breakStage) return "broken";
    // At the breaking stage itself, only the node that actually failed
    // turns red; healthy siblings on the same level stay lit.
    if (ref.stage === breakStage) return ref.selfBroken === false ? "lit" : "broken";
    return "lit";
  }

  // ── Tooltip helpers ──
  function showTooltip(e: React.MouseEvent, title: string, rows: Array<[string, string, Tone]>) {
    const container = containerRef.current;
    if (!container) return;
    const rect = container.getBoundingClientRect();
    setTooltip({
      x: Math.min(e.clientX - rect.left + 18, rect.width - 250),
      y: Math.min(Math.max(e.clientY - rect.top - 12, 10), rect.height - 170),
      title,
      rows,
    });
  }
  function appTooltipRows(app: AppVM): Array<[string, string, Tone]> {
    const stage = (id: string): [string, Tone] => {
      const node = app.nodes.get(id);
      return [String(node?.status ?? "unknown"), (node?.tone as Tone) || "neutral"];
    };
    const [dataStatus, dataTone] = stage("data");
    const [riskStatus, riskTone] = stage("risk");
    const [outputStatus, outputTone] = stage("output");
    const [evidenceStatus, evidenceTone] = stage("evidence");
    return [
      ["Account data", dataStatus, dataTone],
      ["Signal", `${app.signalStatus} · ${relTime(app.signalAt, now)}`, app.signalTone],
      ["Risk", riskStatus, riskTone],
      ["Output", outputStatus, outputTone],
      ["Evidence", evidenceStatus, evidenceTone],
    ];
  }

  function hoverNode(
    e: React.MouseEvent,
    id: string,
    title: string,
    rows: Array<[string, string, Tone]>,
  ) {
    setHoverId(id);
    showTooltip(e, title, rows);
  }
  function leaveNode() {
    setHoverId(null);
    setTooltip(null);
  }

  const animate = !reducedMotion;

  // ═══ Render ═══
  return (
    <div className="pd-frame" ref={containerRef}>
      <svg
        aria-label="Maestro pipeline dashboard"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        viewBox={`0 0 ${VB_W} ${VB_H}`}
      >
        <defs>
          <linearGradient id="pdNodeFill" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#16202f" stopOpacity="0.96" />
            <stop offset="100%" stopColor="#0a111b" stopOpacity="0.94" />
          </linearGradient>
          <linearGradient id="pdNodeSheen" x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#ffffff" stopOpacity="0.05" />
            <stop offset="100%" stopColor="#ffffff" stopOpacity="0" />
          </linearGradient>
          <filter height="300%" id="pdPulseGlow" width="300%" x="-100%" y="-100%">
            <feGaussianBlur stdDeviation="4" />
          </filter>
        </defs>

        {/* ── Column headers ── */}
        {(
          [
            ["DATA SOURCES", COLS.data, allSources.length],
            ["VIRTUOSO APPS", COLS.app, apps.length],
            ["ACCOUNTS", COLS.acct, accts.length],
            ["OPERATING GATE", COLS.gate, 3],
          ] as Array<[string, { x: number; w: number }, number]>
        ).map(([label, col, count]) => (
          <g key={label}>
            <text className="pd-col-label" x={col.x} y={30}>
              {label}
            </text>
            <text className="pd-col-count" x={col.x + col.w} y={30} textAnchor="end">
              {count}
            </text>
            <line className="pd-col-rule" x1={col.x} x2={col.x + col.w} y1={38} y2={38} />
          </g>
        ))}

        {/* ── Edges (base + ambient flow + hover overlays) ── */}
        {edges.map((edge) => {
          const state = edgeState(edge);
          const color = state === "lit" ? FLOW_COLOR : state === "cut" ? CUT_COLOR : toneColor(edge.tone);
          return (
            <g key={edge.key}>
              <path
                className={`pd-edge pd-edge-${state}`}
                d={edge.d}
                fill="none"
                stroke={color}
                strokeLinecap="round"
              />
              {state === "ambient" && animate && edge.tone !== "danger" && (
                <path className="pd-edge-drift" d={edge.d} fill="none" stroke={color} strokeLinecap="round" />
              )}
              {state === "lit" && animate && (
                <RelayPulse color={FLOW_COLOR} path={edge.d} stage={edge.stage} />
              )}
              {state === "cut" && (
                <path className="pd-edge-cut-glow" d={edge.d} fill="none" stroke={CUT_COLOR} strokeLinecap="round" />
              )}
            </g>
          );
        })}

        {/* ── DATA SOURCES ── */}
        {allSources.map((source) => {
          const box = srcPos.get(source.id);
          if (!box) return null;
          const selfBroken =
            source.kind === "broker"
              ? hoveredAppVM
                ? isBroken(hoveredAppVM.nodes.get("data"))
                : undefined
              : source.broken;
          const state = nodeState({ stage: 0, selfBroken, srcAppIds: source.appIds });
          const compact = box.h < 78;
          return (
            <g
              key={source.id}
              className={`pd-node pd-node-${state}`}
              onMouseEnter={(e) => hoverNode(e, `data:${source.id}`, source.title, source.rows)}
              onMouseLeave={leaveNode}
              transform={`translate(${box.x} ${box.y})`}
            >
              <rect className="pd-node-bg" height={box.h} rx={9} width={box.w} />
              <rect fill="url(#pdNodeSheen)" height={24} rx={9} width={box.w - 2} x={1} y={1} />
              <rect className="pd-node-accent" fill={toneColor(source.tone)} height={box.h - 22} rx={1.5} width={3} x={0} y={11} />
              <text className="pd-title" x={16} y={24}>
                {source.title}
              </text>
              <text className="pd-meta" x={16} y={compact ? 38 : 44}>
                {source.meta}
              </text>
              <StatusDot cx={20} cy={box.h - 18} tone={source.tone} blink={source.tone === "danger"} />
              <text className="pd-status" fill={toneColor(source.tone)} x={30} y={box.h - 14.5}>
                {source.statusText}
              </text>
            </g>
          );
        })}
        {dataSources.length > 3 && (
          <text className="pd-more" x={COLS.data.x} y={BAND_BOTTOM + 14}>
            +{dataSources.length - 3} more sources
          </text>
        )}

        {/* ── VIRTUOSO APPS ── */}
        {visibleApps.map((app) => {
          const box = appPos.get(app.id);
          if (!box) return null;
          const state = nodeState({ stage: 1, appId: app.id });
          const overallTone: Tone = app.enabled ? app.signalTone : "neutral";
          const showSpark = app.perf.length >= 2 && box.h >= 104;
          return (
            <g
              key={app.id}
              className={`pd-node pd-node-app pd-node-${state}${app.enabled ? "" : " pd-node-off"}`}
              onClick={() => openApp(app.id)}
              onMouseEnter={(e) => hoverNode(e, `app:${app.id}`, app.name, appTooltipRows(app))}
              onMouseLeave={leaveNode}
              role="button"
              tabIndex={0}
            transform={`translate(${box.x} ${box.y})`}
            >
              <rect className="pd-node-bg" height={box.h} rx={10} width={box.w} />
              <rect fill="url(#pdNodeSheen)" height={26} rx={10} width={box.w - 2} x={1} y={1} />
              <rect className="pd-node-accent" fill={toneColor(overallTone)} height={box.h - 24} rx={2} width={4} x={0} y={12} />
              <text className="pd-title-lg" x={18} y={27}>
                {app.name}
              </text>
              {app.enabled && app.retLabel ? (
                <text className="pd-ret" fill={toneColor(app.retTone)} textAnchor="end" x={box.w - 14} y={27}>
                  {app.retLabel}
                </text>
              ) : !app.enabled ? (
                <g transform={`translate(${box.w - 48} 14)`}>
                  <rect className="pd-off-pill" height={16} rx={8} width={36} />
                  <text className="pd-off-text" textAnchor="middle" x={18} y={11.5}>
                    OFF
                  </text>
                </g>
              ) : null}
              <text className="pd-meta" x={18} y={44}>
                {app.id}
                {app.currency ? ` · ${app.currency}` : ""}
              </text>
              {showSpark && <Sparkline data={app.perf} h={26} tone={app.retTone} w={box.w - 34} x={18} y={52} />}
              <StatusDot
                blink={app.enabled && app.signalTone === "danger"}
                cx={22}
                cy={box.h - 18}
                tone={app.enabled ? app.signalTone : "neutral"}
              />
              <text className="pd-status" fill={toneColor(app.enabled ? app.signalTone : "neutral")} x={32} y={box.h - 14.5}>
                {app.enabled ? `signal ${app.signalStatus} · ${relTime(app.signalAt, now)}` : "signal generation off"}
              </text>
            </g>
          );
        })}
        {apps.length > visibleApps.length && (
          <text className="pd-more" x={COLS.app.x} y={BAND_BOTTOM + 14}>
            +{apps.length - visibleApps.length} more apps
          </text>
        )}

        {/* ── ACCOUNTS ── */}
        {visibleAccts.map((acct) => {
          const box = acctPos.get(acct.id);
          if (!box) return null;
          const state = nodeState({ stage: 2, acctId: acct.id });
          const tone: Tone = acct.row ? ((acct.row.tone as Tone) || "neutral") : "warning";
          const status = acct.row ? String(acct.row.status ?? "unknown") : "no snapshot";
          return (
            <g
              key={acct.id}
              className={`pd-node pd-node-${state}${acct.row ? "" : " pd-node-ghost"}`}
              onMouseEnter={(e) =>
                hoverNode(e, `acct:${acct.id}`, acct.id, [
                  ["Status", status, tone],
                  ["Broker", acct.row ? `${String(acct.row.broker ?? "?")} · ${String(acct.row.environment ?? "?")}` : "n/a", "neutral"],
                  ["Positions", acct.row ? String(acct.row.positions_count ?? 0) : "—", "neutral"],
                  ["Synced", acct.row ? relTime(acct.row.created_at, now) : "never", "neutral"],
                  ["Apps", acct.appIds.join(", ") || "none", "neutral"],
                ])
              }
              onMouseLeave={leaveNode}
              transform={`translate(${box.x} ${box.y})`}
            >
              <rect className="pd-node-bg" height={box.h} rx={9} width={box.w} />
              <rect fill="url(#pdNodeSheen)" height={22} rx={9} width={box.w - 2} x={1} y={1} />
              <rect className="pd-node-accent" fill={toneColor(tone)} height={box.h - 20} rx={1.5} width={3} x={0} y={10} />
              <text className="pd-title-mono" x={15} y={24}>
                {acct.id}
              </text>
              <text className="pd-meta" x={15} y={41}>
                {acct.row ? `${String(acct.row.broker ?? "?")} · ${String(acct.row.environment ?? "?")}` : "mapped · not synced"}
              </text>
              <StatusDot cx={19} cy={box.h - 18} tone={tone} blink={tone === "danger"} />
              <text className="pd-status" fill={toneColor(tone)} x={29} y={box.h - 14.5}>
                {status}
                {acct.row ? ` · ${String(acct.row.positions_count ?? 0)} pos` : ""}
              </text>
            </g>
          );
        })}
        {accts.length > visibleAccts.length && (
          <text className="pd-more" x={COLS.acct.x} y={BAND_BOTTOM + 14}>
            +{accts.length - visibleAccts.length} more accounts
          </text>
        )}

        {/* ── OPERATING GATE ── */}
        {(
          [
            {
              id: "risk",
              box: gatePos.risk,
              kicker: "GATE 1",
              title: "Risk Gate",
              tone: (riskNode?.tone as Tone) || "neutral",
              lines: [
                String(riskNode?.status ?? "unknown"),
                relTime(riskNode?.updated_at, now),
              ],
              rows: [
                ["Decision", String(riskNode?.status ?? "unknown"), (riskNode?.tone as Tone) || "neutral"],
                ["Detail", String(riskNode?.detail ?? "n/a"), "neutral"],
                ["Updated", relTime(riskNode?.updated_at, now), "neutral"],
              ] as Array<[string, string, Tone]>,
            },
            {
              id: "exec",
              box: gatePos.exec,
              kicker: "GATE 2",
              title: "Execution",
              tone:
                orderPosture === "armed"
                  ? "warning"
                  : ((outputNode?.tone as Tone) || "neutral"),
              lines: [`posture ${orderPosture}`, mode.replace(/_/g, " ")],
              rows: [
                ["Posture", orderPosture, orderPosture === "armed" ? "warning" : "neutral"],
                ["Mode", mode, "neutral"],
                ["Output", String(outputNode?.status ?? "n/a"), (outputNode?.tone as Tone) || "neutral"],
              ] as Array<[string, string, Tone]>,
            },
            {
              id: "state",
              box: gatePos.state,
              kicker: "GATE 3",
              title: "State & Audit",
              tone: (stateNode?.tone as Tone) || "neutral",
              lines: [String(stateNode?.status ?? "unknown"), `published ${publishedState}`],
              rows: [
                ["Runs", String(stateNode?.status ?? "unknown"), "neutral"],
                ["Published", publishedState, toneFromValue(publishedState)],
                ["Audit", String(findCheck(checks, "audit_integrity")?.message ?? "n/a"), toneFromValue(findCheck(checks, "audit_integrity")?.status)],
              ] as Array<[string, string, Tone]>,
            },
          ] as const
        ).map((gate, i) => {
          const state = nodeState({ stage: 3 + i });
          return (
            <g
              key={gate.id}
              className={`pd-node pd-node-${state}`}
              onMouseEnter={(e) => hoverNode(e, `gate:${gate.id}`, gate.title, [...gate.rows])}
              onMouseLeave={leaveNode}
              transform={`translate(${gate.box.x} ${gate.box.y})`}
            >
              <rect className="pd-node-bg" height={gate.box.h} rx={9} width={gate.box.w} />
              <rect fill="url(#pdNodeSheen)" height={24} rx={9} width={gate.box.w - 2} x={1} y={1} />
              <rect className="pd-node-accent" fill={toneColor(gate.tone)} height={gate.box.h - 22} rx={1.5} width={3} x={0} y={11} />
              <text className="pd-kicker" x={16} y={20}>
                {gate.kicker}
              </text>
              <text className="pd-title" x={16} y={38}>
                {gate.title}
              </text>
              <StatusDot cx={20} cy={58} tone={gate.tone} blink={gate.tone === "danger"} />
              <text className="pd-status" fill={toneColor(gate.tone)} x={30} y={61.5}>
                {gate.lines[0]}
              </text>
              <text className="pd-meta" x={16} y={gate.box.h - 14}>
                {gate.lines[1]}
              </text>
            </g>
          );
        })}

        {/* ── System rail ── */}
        <line className="pd-rail-rule" x1={24} x2={VB_W - 24} y1={RAIL_Y - 12} y2={RAIL_Y - 12} />
        <text className="pd-col-label" x={24} y={RAIL_Y + 8}>
          SYSTEM
        </text>
        {(
          [
            ["heartbeat", "heartbeat"],
            ["scheduled_run", "scheduled run"],
            ["reconciliation", "reconciliation"],
            ["audit_integrity", "audit"],
          ] as const
        ).map(([name, label], i) => {
          const check = findCheck(checks, name);
          const tone = check ? toneFromValue(check.status === "warn" ? "warning" : check.status) : "neutral";
          return (
            <g key={name} transform={`translate(${104 + i * 168} ${RAIL_Y})`}>
              <StatusDot cx={6} cy={4} tone={tone} blink={tone === "danger"} />
              <text className="pd-rail-text" x={16} y={7.5}>
                {label} · {String(check?.message ?? check?.status ?? "n/a")}
              </text>
            </g>
          );
        })}
        {/* Legend */}
        <g transform={`translate(${VB_W - 292} ${RAIL_Y})`}>
          <line stroke={FLOW_COLOR} strokeLinecap="round" strokeWidth={2.5} x1={0} x2={18} y1={4} y2={4} opacity={0.9} />
          <text className="pd-rail-text" x={24} y={7.5}>
            active path
          </text>
          <line stroke={CUT_COLOR} strokeLinecap="round" strokeWidth={2.5} x1={96} x2={114} y1={4} y2={4} opacity={0.9} />
          <text className="pd-rail-text" x={120} y={7.5}>
            blocked
          </text>
          <circle cx={182} cy={4} fill="var(--green)" r={3} />
          <text className="pd-rail-text" x={190} y={7.5}>
            fresh
          </text>
          <circle cx={230} cy={4} fill="var(--amber)" r={3} />
          <text className="pd-rail-text" x={238} y={7.5}>
            stale
          </text>
        </g>
      </svg>

      {/* ── Tooltip ── */}
      {tooltip && (
        <div className="pd-tooltip" style={{ left: tooltip.x, top: tooltip.y }}>
          <div className="pd-tooltip-title">{tooltip.title}</div>
          {tooltip.rows.map(([label, value, tone]) => (
            <div className="pd-tooltip-row" key={label}>
              <span className="pd-tooltip-label">{label}</span>
              <span className="pd-tooltip-value" style={{ color: tone === "neutral" ? "var(--text)" : toneColor(tone) }}>
                {value}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
