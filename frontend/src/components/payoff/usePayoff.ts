"use client";
import { useEffect, useRef, useState } from "react";
import { runAgent } from "@/lib/api";
import { streamAgent } from "@/lib/agentStream";
import type { StreamEvent } from "@/lib/agentActivity";
import { useAgentActivity } from "@/lib/useAgentActivity";
import { toView, type PayoffView, type StrategyCode } from "@/lib/payoff";

/** What the customer has touched. null = untouched: the backend's default applies. 0 is a real choice meaning "none". */
export type Controls = { extra: number | null; lump: number | null; strategy: StrategyCode | null };
export const UNTOUCHED: Controls = { extra: null, lump: null, strategy: null };
export type Defaults = { extra: number; lump: number; recommended: StrategyCode | null };

const DEBOUNCE_MS = 400;
const RETRY_MS = 500;

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((res, rej) => {
  const t = setTimeout(res, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); rej(new DOMException("aborted", "AbortError")); }, { once: true });
});

/** One simulate_payoff call. With `live`, it goes over the stream endpoint (one request: the final "result" event is the answer) and
 *  reports progress. A 409 (a newer run replaced this one) is retried once after 500 ms. */
async function fetchView(c: Controls, signal: AbortSignal, live?: { begin: () => void; onEvent: (e: StreamEvent) => void; finish: () => void }): Promise<PayoffView> {
  const params = { extra_monthly: c.extra, lump_sum: c.lump, strategy: c.strategy };
  const once = async () => {
    if (!live) return runAgent("simulate_payoff", { params, signal });
    live.begin();
    try { return await streamAgent("simulate_payoff", { params, signal, onEvent: (e) => { if (!signal.aborted) live.onEvent(e); } }); }
    finally { live.finish(); }
  };
  let r = await once();
  if (!r) { await sleep(RETRY_MS, signal); r = await once(); }
  if (!r?.result.payoff) throw new Error("no payoff result");
  return toView(r.result.payoff);
}

/** Scenario state lives here (React state, never the URL). Requests are debounced, stale ones are cancelled, and the last good result
 *  stays available while a new one loads or fails. */
export function usePayoff() {
  const [controls, setControls] = useState<Controls>(UNTOUCHED);
  const [last, setLast] = useState<{ key: string; view: PayoffView } | null>(null);
  const [failedKey, setFailedKey] = useState<string | null>(null);
  const [defaults, setDefaults] = useState<Defaults | null>(null);
  const loadedOnce = useRef(false);
  const agent = useAgentActivity();
  const { begin, onEvent, finish } = agent;
  const key = JSON.stringify(controls);

  useEffect(() => {
    const ctl = new AbortController();
    const c: Controls = JSON.parse(key);
    const timer = setTimeout(async () => {
      try {
        const view = await fetchView(c, ctl.signal, { begin, onEvent, finish });
        if (ctl.signal.aborted) return;
        loadedOnce.current = true;
        setLast({ key, view });
        setFailedKey(null);
        if (c.extra === null && c.lump === null && c.strategy === null) setDefaults({ extra: view.extraMonthly, lump: view.safeLumpSum, recommended: view.recommended });
      } catch {
        if (!ctl.signal.aborted) setFailedKey(key);
      }
    }, loadedOnce.current ? DEBOUNCE_MS : 0);
    return () => { clearTimeout(timer); ctl.abort(); };
  }, [key, begin, onEvent, finish]);

  return {
    agent,
    controls,
    set: (patch: Partial<Controls>) => setControls((c) => ({ ...c, ...patch })),
    reset: () => setControls(UNTOUCHED),
    view: last?.view ?? null,
    defaults,
    loading: last?.key !== key && failedKey !== key,
    failed: failedKey === key,
    firstFailed: failedKey === key && !last,
  };
}

/** The minimum-payments-only schedule, fetched once, as the dashed reference on the balance chart. */
export function useMinimumOnly(enabled: boolean) {
  const [view, setView] = useState<PayoffView | null>(null);
  useEffect(() => {
    if (!enabled || view) return;       // once is enough: the minimum-payments schedule does not depend on the controls
    const ctl = new AbortController();
    fetchView({ extra: null, lump: null, strategy: "E" }, ctl.signal).then((v) => { if (!ctl.signal.aborted) setView(v); }).catch(() => {});
    return () => ctl.abort();
  }, [enabled, view]);
  return view;
}
