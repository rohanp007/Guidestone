/**
 * Typed fetch wrappers for the Guidestone API. Types come from the generated api-types.ts (npm run gen:api),
 * so a change to the backend models shows up here as a compile error instead of a runtime surprise.
 */
import type { components, paths } from "./api-types";

type Schemas = components["schemas"];
export type Health = Schemas["Health"];
export type Profile = Schemas["Profile"];
export type ProfileListItem = Schemas["ProfileListItem"];
export type Transaction = Schemas["Transaction"];
export type TransactionPage = Schemas["TransactionPage"];
export type FormulaInfo = Schemas["FormulaInfo"];
export type AssumptionsResponse = Schemas["AssumptionsResponse"];
export type LiquidForecast = Schemas["LiquidForecast"];
export type PaycheckToPaycheck = Schemas["PaycheckToPaycheck"];
export type ForecastArgs = Schemas["ForecastArgs"];

export type TransactionQuery = NonNullable<paths["/api/profiles/{id}/transactions"]["get"]["parameters"]["query"]>;

const BASE = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/** A non-2xx response. `detail` is the API's message (a string, or a list of field errors for a 422). */
export class ApiError extends Error {
  constructor(public status: number, public detail: unknown, public formula?: string) {
    super(typeof detail === "string" ? detail : `API error ${status}`);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<{ data: T; headers: Headers }> {
  const res = await fetch(`${BASE}${path}`, init);
  const body = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? res.statusText, body?.formula);
  return { data: body as T, headers: res.headers };
}

const query = (params: Record<string, unknown> = {}) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
};

export const getHealth = () => request<Health>("/api/health").then((r) => r.data);
export const getAssumptions = () => request<AssumptionsResponse>("/api/assumptions").then((r) => r.data);
export const listProfiles = () => request<ProfileListItem[]>("/api/profiles").then((r) => r.data);
export const getProfile = (id: string) => request<Profile>(`/api/profiles/${encodeURIComponent(id)}`).then((r) => r.data);
export const getTransactions = (id: string, params: TransactionQuery = {}) =>
  request<TransactionPage>(`/api/profiles/${encodeURIComponent(id)}/transactions${query(params)}`).then((r) => r.data);
export const listFormulas = () => request<FormulaInfo[]>("/api/formulas").then((r) => r.data);

/** Run a registered formula. `computeMs` and `cached` come from the X-Compute-Ms / X-Cache headers (shown on the debug page). */
export async function runFormula<T = unknown>(id: string, name: string, args: Record<string, unknown> = {}) {
  const { data, headers } = await request<T>(`/api/profiles/${encodeURIComponent(id)}/formulas/${encodeURIComponent(name)}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(args),
  });
  return { result: data, computeMs: Number(headers.get("X-Compute-Ms")), cached: headers.get("X-Cache") === "hit" };
}

export const forecastLiquid = (id: string, args: ForecastArgs = {}) => runFormula<LiquidForecast>(id, "forecast_liquid", args);
export const forecastLiquidSummary = (id: string, args: ForecastArgs = {}) => runFormula<LiquidForecast>(id, "forecast_liquid_summary", args);
export const paycheckToPaycheck = (id: string) => runFormula<PaycheckToPaycheck>(id, "paycheck_to_paycheck");
