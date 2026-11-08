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
export type Me = Schemas["Me"];
export type DemoPersona = Schemas["DemoPersona"];
export type AnalysisResult = Schemas["AnalysisResult"];
export type MyStatus = Schemas["Status"];
export type RecurringItem = Schemas["RecurringItem"];
export type Advice = NonNullable<AnalysisResult["advice"]>;

export type TransactionQuery = NonNullable<paths["/api/profiles/{id}/transactions"]["get"]["parameters"]["query"]>;

const BASE = "";   // same-origin: next.config.ts rewrites /api/* to the backend

/** A non-2xx response. `detail` is the API's message (a string, or a list of field errors for a 422). */
export class ApiError extends Error {
  constructor(public status: number, public detail: unknown, public formula?: string) {
    super(typeof detail === "string" ? detail : `API error ${status}`);
  }
}

/** Where an expired or missing session sends the browser. The login page itself must not redirect to itself. */
// eslint-disable-next-line @next/next/no-location-assign-relative-destination -- outside React; a full navigation also drops any signed-in state
const toLogin = () => { if (typeof window !== "undefined" && window.location.pathname !== "/login") window.location.assign("/login?expired=1"); };

async function request<T>(path: string, init?: RequestInit, opts: { redirectOn401?: boolean } = {}): Promise<{ data: T; headers: Headers }> {
  // the session is an httpOnly cookie: the browser attaches it, scripts never see it
  const res = await fetch(`${BASE}${path}`, { ...init, credentials: "same-origin" });
  if (res.status === 401 && opts.redirectOn401 !== false) toLogin();
  const body = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(res.status, body?.detail ?? res.statusText, body?.formula);
  return { data: body as T, headers: res.headers };
}

const query = (params: Record<string, unknown> = {}) => {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") q.set(k, String(v));
  const s = q.toString();
  return s ? `?${s}` : "";
};

/** Who is signed in. Pass `redirect: false` to get the 401 back instead of being sent to /login (used by the login page). */
export const getMe = (redirect = true) => request<Me>("/api/me", undefined, { redirectOn401: redirect }).then((r) => r.data);
export const login = (email: string, password: string) =>
  request<{ customer_id: string; first_name: string }>("/api/auth/login", {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email, password }),
  }, { redirectOn401: false }).then((r) => r.data);
export const logout = () => request<null>("/api/auth/logout", { method: "POST" }, { redirectOn401: false }).then(() => undefined);
/** Demo mode only (404 otherwise): the featured customers, as email prefills for the login form. */
export const demoPersonas = () => request<DemoPersona[]>("/api/auth/demo-personas", undefined, { redirectOn401: false }).then((r) => r.data);

/** The signed-in customer's own data (no id in the URL: the session decides who it is). */
export const getMyProfile = () => request<Profile>("/api/me/profile").then((r) => r.data);
export const getMyForecast = () => request<LiquidForecast>("/api/me/forecast").then((r) => r.data);
export const getMyStatus = () => request<MyStatus>("/api/me/status").then((r) => r.data);
export const getMyRecurring = () => request<RecurringItem[]>("/api/me/recurring").then((r) => r.data);
export const getMyTransactions = (params: { start?: string; end?: string; category?: string; q?: string; page?: number; page_size?: number } = {}) =>
  request<TransactionPage>(`/api/me/transactions${query(params)}`).then((r) => r.data);

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

/** Run an orchestrator event for the signed-in customer. Returns null when a newer run replaced this one (409): that is expected
 *  (the sign-in warm-up, a refresh), so callers just ignore it and use the newer run's result instead of showing an error. */
export async function runAgent(event: string, opts: { refresh?: boolean; params?: Record<string, unknown> } = {}): Promise<{ result: AnalysisResult; cached: boolean } | null> {
  try {
    const { data, headers } = await request<AnalysisResult>(`/api/me/agent/${encodeURIComponent(event)}${query({ refresh: opts.refresh ? 1 : undefined })}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.params ?? {}),
    });
    return { result: data, cached: headers.get("X-Cache") === "hit" };
  } catch (e) {
    if (e instanceof ApiError && e.status === 409) return null;
    throw e;
  }
}
