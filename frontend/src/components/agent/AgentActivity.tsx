"use client";
import { useState } from "react";
import { AGENT_TITLE, elapsedMs, pillText, seconds, statusText, type Activity, type RowStatus } from "@/lib/agentActivity";
import type { AgentActivityHandle } from "@/lib/useAgentActivity";

const COLOR: Record<RowStatus | "cached", string> = {
  waiting: "var(--ink-3)", running: "var(--navy)", done: "var(--good)", retried: "var(--fair)", standard: "var(--fair)", error: "var(--poor)", cached: "var(--good)",
};

/** Every status is an icon AND a text label beside it, never colour alone. */
function StatusIcon({ status }: { status: RowStatus | "cached" }) {
  const c = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: COLOR[status], strokeWidth: 1.75, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true, className: "shrink-0" } as const;
  switch (status) {
    case "waiting": return <svg {...c}><circle cx="8" cy="8" r="6.25" strokeDasharray="2 3" /></svg>;
    case "running": return <svg {...c}><circle cx="8" cy="8" r="6.25" opacity="0.25" /><path className="spin" d="M8 1.75a6.25 6.25 0 0 1 6.25 6.25" /></svg>;
    case "retried": return <svg {...c}><path d="M13 8a5 5 0 1 1-1.6-3.7" /><path d="M13 2.5v3h-3" /></svg>;
    case "standard": return <svg {...c}><path d="M4 2.5h6l2.5 2.5v8.5H4z" /><path d="M6.2 8h3.6M6.2 10.5h3.6" /></svg>;
    case "error": return <svg {...c}><path d="M8 1.8l6.4 11.4H1.6z" /><path d="M8 6.4v3.2M8 11.4v.1" /></svg>;
    default: return <svg {...c}><circle cx="8" cy="8" r="6.25" /><path d="M5.2 8.2l2 2 3.6-4" /></svg>;
  }
}

const Chevron = ({ open }: { open: boolean }) => (
  <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden
    style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 200ms var(--ease)" }}><path d="M3 5l4 4 4-4" /></svg>
);

function overall(a: Activity): RowStatus | "cached" {
  if (a.phase === "running") return "running";
  if (a.phase === "error" || a.phase === "replaced") return "error";
  return a.cached ? "cached" : "done";
}

function Body({ h }: { h: AgentActivityHandle }) {
  const { activity: a, current, now } = h;
  return (
    <div className="rise-in">
      {a.cached && a.phase === "done" && <p className="mb-3 text-[14px] text-ink-2">From earlier run</p>}
      {a.message && <p className="mb-3 text-[14px] text-ink">{a.message}</p>}
      <ul className="space-y-4">
        {a.rows.map((r) => {
          const t = elapsedMs(a, r, now);
          const now_ = current[r.agent];
          return (
            <li key={r.agent}>
              <div className="flex items-center justify-between gap-3">
                <span className="flex items-center gap-2 text-[15px] font-medium"><StatusIcon status={a.cached && r.status === "done" ? "cached" : r.status} />{AGENT_TITLE[r.agent]}</span>
                <span className="num text-[13px] text-ink-2">{t === null ? "" : seconds(t)}</span>
              </div>
              <p className="ml-6 text-[13px] text-ink-2">{statusText(r)}</p>
              {(r.steps.length > 0 || now_) && (
                <ul className="ml-6 mt-1.5 space-y-1 text-[14px]">
                  {r.steps.map((s, i) => <li key={`${i}-${s}`} className="flex items-start gap-2"><span className="mt-1 shrink-0"><StatusIcon status="done" /></span><span>{s}</span></li>)}
                  {now_ && <li className="flex items-start gap-2 text-ink-2"><span className="mt-1 shrink-0"><StatusIcon status="running" /></span><span>{now_}</span></li>}
                </ul>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

/** What the agents are doing for this request: one row per agent with status, timer and step labels, collapsing to a summary pill when
 *  the run finishes. `panel` (desktop side panel), `sheet` (mobile bottom sheet) and `inline` (inside a card) differ only in frame.
 *  Shows only the backend's fixed labels: no prompts, model text or customer data ever reach it. */
export default function AgentActivity({ h, variant }: { h: AgentActivityHandle; variant: "panel" | "sheet" | "inline" }) {
  const a = h.activity;
  const [manual, setManual] = useState<{ run: number | null; open: boolean } | null>(null);
  if (a.phase === "idle") return null;
  const open = manual && manual.run === a.startedAt ? manual.open : a.phase === "running";      // open while it runs, a pill once it is done
  const toggle = () => setManual({ run: a.startedAt, open: !open });
  const announce = a.phase === "running" ? h.latest : pillText(a, h.now);

  const header = (
    <button type="button" onClick={toggle} aria-expanded={open} className="press flex w-full items-center justify-between gap-3 text-left">
      <span className="flex items-center gap-2 text-[14px] font-medium"><StatusIcon status={overall(a)} /><span>{pillText(a, h.now)}</span></span>
      <span className="text-ink-2"><Chevron open={open} /></span>
    </button>
  );
  const live = <p className="sr-only" aria-live="polite">{announce}</p>;

  if (variant === "sheet") {
    return (
      <section aria-label="Agent activity" className="fixed inset-x-0 bottom-0 z-30 rounded-t-[6px] border-t border-line-strong bg-surface px-4 pb-3 pt-3">
        {live}{header}
        {open && <div className="mt-3 max-h-[55vh] overflow-auto pb-1">{<Body h={h} />}</div>}
      </section>
    );
  }
  if (variant === "inline") {          // inside a card that already has a frame: a hairline, no box of its own
    return (
      <section aria-label="Agent activity" className="mt-5 border-t border-line pt-3">
        {live}{header}
        {open && <div className="mt-3"><Body h={h} /></div>}
      </section>
    );
  }
  return (
    <section aria-label="Agent activity" className="rounded-[6px] border border-line-strong bg-surface">
      {live}
      <div className="px-4 py-3">{header}</div>
      {open && <div className="border-t border-line px-4 py-4"><Body h={h} /></div>}
    </section>
  );
}
