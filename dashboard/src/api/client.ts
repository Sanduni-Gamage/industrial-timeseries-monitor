/**
 * Typed API client.
 *
 * Response types come from `schema.d.ts`, which is generated from the FastAPI
 * application's own OpenAPI document (`npm run gen:types`). That is what keeps the
 * contract honest: if a Python response model changes and the dashboard is not updated,
 * the TypeScript build fails rather than the UI quietly rendering `undefined`.
 *
 * Errors are RFC 7807 problem documents. They are surfaced as a typed `ApiError` so the
 * UI can show the server's own explanation - which is written for an operator - instead
 * of a generic "something went wrong".
 */

import type { components } from "./schema";

type Schemas = components["schemas"];

export type Equipment = Schemas["Equipment"];
export type Sensor = Schemas["Sensor"];
export type ReadingSeries = Schemas["ReadingSeries"];
export type TrendSeries = Schemas["TrendSeries"];
export type Anomaly = Schemas["Anomaly"];
export type AnomalyPage = Schemas["AnomalyPage"];
export type FailureEvent = Schemas["FailureEvent"];
export type EquipmentHealth = Schemas["EquipmentHealth"];
export type SystemSummary = Schemas["SystemSummary"];
export type DataQualitySummary = Schemas["DataQualitySummary"];
export type HealthResponse = Schemas["HealthResponse"];
export type ProblemDetail = Schemas["ProblemDetail"];

export type Severity = "NORMAL" | "WARNING" | "CRITICAL";
export type OperatingState = "OFF" | "OFFLOADED" | "LOADED" | "STARTING";
export type Resolution = "auto" | "raw" | "hourly" | "daily";

/** Same-origin in dev (Vite proxies /api) and in a built deployment behind a reverse proxy. */
const BASE = "/api";

export class ApiError extends Error {
  readonly status: number;
  readonly problem?: ProblemDetail;

  constructor(status: number, message: string, problem?: ProblemDetail) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.problem = problem;
  }

  /** Whether retrying might help. Drives whether the UI offers a Retry button. */
  get isTransient(): boolean {
    return this.status === 503 || this.status >= 500;
  }
}

type QueryValue = string | number | boolean | null | undefined;

function buildUrl(path: string, params?: Record<string, QueryValue>): string {
  const url = new URL(BASE + path, window.location.origin);
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value !== undefined && value !== null && value !== "") {
      url.searchParams.set(key, String(value));
    }
  }
  return url.pathname + url.search;
}

async function request<T>(
  path: string,
  params?: Record<string, QueryValue>,
  signal?: AbortSignal,
): Promise<T> {
  let response: Response;
  try {
    response = await fetch(buildUrl(path, params), {
      signal,
      headers: { Accept: "application/json" },
    });
  } catch (cause) {
    if ((cause as Error).name === "AbortError") throw cause;
    // A network-level failure is the one case where the server cannot explain itself,
    // so the message has to be written here - and it should say what to check.
    throw new ApiError(
      0,
      "Could not reach the monitoring service. Check that the API is running on port 8000.",
    );
  }

  if (!response.ok) {
    let problem: ProblemDetail | undefined;
    try {
      problem = (await response.json()) as ProblemDetail;
    } catch {
      /* a non-JSON error body is possible from a proxy; fall through to the status text */
    }
    throw new ApiError(
      response.status,
      problem?.detail ?? `Request failed with status ${response.status}.`,
      problem,
    );
  }

  return (await response.json()) as T;
}

export const api = {
  health: (signal?: AbortSignal) => request<HealthResponse>("/health", undefined, signal),

  summary: (signal?: AbortSignal) => request<SystemSummary>("/summary", undefined, signal),

  equipment: (signal?: AbortSignal) => request<Equipment[]>("/equipment", undefined, signal),

  equipmentHealth: (id: number, signal?: AbortSignal) =>
    request<EquipmentHealth>(`/equipment/${id}/health`, undefined, signal),

  sensors: (params?: { equipment_id?: number; sensor_class?: string }, signal?: AbortSignal) =>
    request<Sensor[]>("/sensors", params, signal),

  readings: (
    sensor: string,
    params: { start?: string; end?: string; resolution?: Resolution },
    signal?: AbortSignal,
  ) => request<ReadingSeries>(`/readings/${encodeURIComponent(sensor)}`, params, signal),

  trend: (
    sensor: string,
    params: {
      start?: string;
      end?: string;
      operating_state?: OperatingState;
      window_hours?: number;
    },
    signal?: AbortSignal,
  ) => request<TrendSeries>(`/trends/${encodeURIComponent(sensor)}`, params, signal),

  anomalies: (
    params: {
      start?: string;
      end?: string;
      sensor_id?: number;
      severity?: Severity;
      method?: string;
      limit?: number;
      offset?: number;
    },
    signal?: AbortSignal,
  ) => request<AnomalyPage>("/anomalies", params, signal),

  failures: (signal?: AbortSignal) => request<FailureEvent[]>("/failures", undefined, signal),

  dataQuality: (signal?: AbortSignal) =>
    request<DataQualitySummary>("/data-quality", undefined, signal),
};
