/**
 * What the AgentActivity window shows, built from the backend's stream events (POST /api/me/agent/{event}/stream).
 * Pure state: no React, no fetch. Labels are the backend's fixed catalog; no model text ever arrives here.
 */
import type { components } from "./api-types";

export type StreamEvent = components["schemas"]["StreamEvent"];
export type AgentName = "orchestrator" | "compute" | "advisor" | "presenter" | "researcher";
export type RowStatus = "waiting" | "running" | "done" | "retried" | "standard" | "error";

export const AGENT_ORDER: AgentName[] = ["orchestrator", "compute", "advisor", "presenter", "researcher"];
export const AGENT_TITLE: Record<AgentName, string> = { orchestrator: "Orchestrator", compute: "Compute", advisor: "Advisor", presenter: "Presenter", researcher: "Researcher" };
const ALWAYS: AgentName[] = ["orchestrator", "compute", "advisor"];

export const RETRY_LABEL = "Retrying with stricter wording";
export const STANDARD_LABEL = "Using the standard wording";
export const REPLACED_LABEL = "A newer check-up replaced this one";
export const ERROR_MESSAGE = "Something went wrong; showing the standard view";

export type Row = { agent: AgentName; status: RowStatus; steps: string[]; startedAt: number | null; endedAt: number | null; retried: boolean; standard: boolean };
export type Activity = {
  phase: "idle" | "running" | "done" | "error" | "replaced";
  cached: boolean;
  rows: Row[];
  calcs: number;
  startedAt: number | null;
  endedAt: number | null;
  message: string | null;
};
export type Action = { type: "reset" } | { type: "begin"; now: number } | { type: "event"; ev: StreamEvent; now: number };

const row = (agent: AgentName): Row => ({ agent, status: "waiting", steps: [], startedAt: null, endedAt: null, retried: false, standard: false });
export const IDLE: Activity = { phase: "idle", cached: false, rows: [], calcs: 0, startedAt: null, endedAt: null, message: null };
const fresh = (now: number): Activity => ({ ...IDLE, phase: "running", rows: ALWAYS.map(row), startedAt: now });

const finished = (s: RowStatus) => s === "done" || s === "retried" || s === "standard" || s === "error";
const settled = (r: Row): RowStatus => (r.standard ? "standard" : r.retried ? "retried" : "done");

export function reduce(state: Activity, a: Action): Activity {
  if (a.type === "reset") return IDLE;
  if (a.type === "begin") return fresh(a.now);
  const { ev, now } = a;
  const s = state.phase === "running" ? state : fresh(now);
  if (ev.type === "result") {
    const rows = s.rows.map((r) => (finished(r.status) || r.status === "waiting" ? r : { ...r, status: settled(r), endedAt: now }))
      .filter((r) => r.startedAt !== null || r.steps.length > 0);
    return { ...s, phase: "done", rows, endedAt: now, message: null };
  }
  if (ev.type === "error") {
    const replaced = ev.label === REPLACED_LABEL;
    const rows = s.rows.map((r) => (finished(r.status) ? r : { ...r, status: "error" as RowStatus, endedAt: now }));
    return { ...s, phase: replaced ? "replaced" : "error", rows, endedAt: now, message: replaced ? REPLACED_LABEL : ERROR_MESSAGE };
  }
  const agent = ev.agent as AgentName | null | undefined;
  if (!agent || !ev.label) return s;
  const cached = s.cached || ev.detail?.cached === true;       // a replay has no timers: every step is already done
  let rows = s.rows;
  if (!rows.some((r) => r.agent === agent)) rows = AGENT_ORDER.flatMap((n) => rows.find((r) => r.agent === n) ?? (n === agent ? [row(n)] : []));
  let calcs = s.calcs;
  rows = rows.map((r) => {
    if (r.agent !== agent) return r;
    const n = { ...r, steps: [...r.steps], startedAt: r.startedAt ?? now };
    const label = ev.label as string;
    if (ev.phase !== "start" && n.steps[n.steps.length - 1] !== label) n.steps.push(label);
    if (label === RETRY_LABEL) n.retried = true;
    if (label === STANDARD_LABEL) n.standard = true;
    if (agent === "compute" && ev.phase === "step" && ev.detail?.formula) calcs++;
    if (ev.phase === "start") { if (!finished(n.status)) n.status = "running"; }
    else if (ev.phase === "step") { if (n.status === "waiting") n.status = "running"; }
    else if (ev.phase === "done") { n.status = settled(n); n.endedAt = now; }
    else if (ev.phase === "error") { n.status = "error"; n.endedAt = now; }
    return cached && ev.phase !== "start" ? { ...n, status: finished(n.status) ? n.status : settled(n), endedAt: now } : n;
  });
  return { ...s, cached, rows, calcs };
}

/** Elapsed time of a row (or the whole run), or null for a cached replay (no timers). */
export function elapsedMs(state: Activity, r: Row | null, now: number): number | null {
  if (state.cached) return null;
  const start = r ? r.startedAt : state.startedAt;
  if (start === null) return null;
  return Math.max(0, (r ? r.endedAt : state.endedAt) ?? now) - start;
}

export const seconds = (ms: number) => `${(ms / 1000).toFixed(1)} s`;

const plural = (n: number, w: string) => `${n} ${w}${n === 1 ? "" : "s"}`;

/** The collapsed pill's text. */
export function pillText(s: Activity, now: number): string {
  if (s.phase === "error") return ERROR_MESSAGE;
  if (s.phase === "replaced") return s.message ?? REPLACED_LABEL;
  if (s.cached) return "From earlier run";
  const agents = s.rows.length;
  const t = elapsedMs(s, null, now);
  const base = `${plural(agents, "agent")} · ${plural(s.calcs, "calculation")}`;
  return s.phase === "running" ? `Working · ${base}` : `${base}${t === null ? "" : ` · ${seconds(t)}`}`;
}

export const STATUS_TEXT: Record<RowStatus, string> = { waiting: "Waiting", running: "Running", done: "Done", retried: "Done after a retry", standard: "Standard wording", error: "Stopped" };
export const statusText = (r: Row) => (r.status === "running" && r.retried ? "Retrying" : STATUS_TEXT[r.status]);
