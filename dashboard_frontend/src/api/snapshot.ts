import type { DashboardRefreshResponse, DashboardSnapshot, GenerateSignalResponse } from "../types";

export async function fetchSnapshot(currency: "KRW" | "USD"): Promise<DashboardSnapshot> {
  const response = await fetch(`/api/dashboard/snapshot?display_currency=${currency}`);
  if (!response.ok) {
    throw new Error(await dashboardErrorMessage(response, "Dashboard snapshot is unavailable"));
  }
  return (await response.json()) as DashboardSnapshot;
}

export async function loadSnapshot(
  currency: "KRW" | "USD",
  setSnapshot: (snapshot: DashboardSnapshot) => void,
  setLoading: (loading: boolean) => void,
  setError: (error: string | null) => void,
) {
  setLoading(true);
  setError(null);
  try {
    setSnapshot(await fetchSnapshot(currency));
  } catch (error) {
    setError(error instanceof Error ? error.message : "Unknown dashboard error");
  } finally {
    setLoading(false);
  }
}

export async function refreshDashboardState(): Promise<DashboardRefreshResponse> {
  const response = await fetch("/api/dashboard/refresh", { method: "POST" });
  if (!response.ok) {
    throw new Error(await dashboardErrorMessage(response, "Dashboard refresh failed"));
  }
  return (await response.json()) as DashboardRefreshResponse;
}

export async function generateStrategySignal(strategyId: string): Promise<GenerateSignalResponse> {
  const response = await fetch(
    `/api/dashboard/virtuoso/${encodeURIComponent(strategyId)}/generate-signal`,
    { method: "POST" },
  );
  if (!response.ok) {
    throw new Error(await dashboardErrorMessage(response, "Signal generation failed"));
  }
  return (await response.json()) as GenerateSignalResponse;
}

async function dashboardErrorMessage(response: Response, fallback: string): Promise<string> {
  try {
    const payload = (await response.json()) as {
      detail?: string | { message?: string; status?: string };
      message?: string;
    };
    if (typeof payload.detail === "string") {
      return payload.detail;
    }
    if (payload.detail?.message) {
      return payload.detail.message;
    }
    if (payload.message) {
      return payload.message;
    }
  } catch {
    // Keep the fallback below for non-JSON server errors.
  }
  return `${fallback} (${response.status}).`;
}
