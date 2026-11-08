"use client";
import { useCallback, useEffect, useState } from "react";

import { streamAgent } from "@/lib/agentStream";
import { useAgentActivity } from "@/lib/useAgentActivity";
import { toBuyView, type BuyParams, type BuyView } from "@/lib/buy";

const sleep = (ms: number, signal: AbortSignal) => new Promise<void>((res, rej) => {
  const t = setTimeout(res, ms);
  signal.addEventListener("abort", () => { clearTimeout(t); rej(new DOMException("aborted", "AbortError")); }, { once: true });
});

/** Runs simulate_buy over the stream endpoint (one request; the activity window follows it). The last good result stays until a new one
 *  arrives, and a failed run leaves it (and the form) usable. */
export function useBuy() {
  const agent = useAgentActivity();
  const { begin, onEvent, finish } = agent;
  const [req, setReq] = useState<{ id: number; params: BuyParams } | null>(null);
  const [last, setLast] = useState<{ id: number; view: BuyView; params: BuyParams } | null>(null);
  const [failedId, setFailedId] = useState<number | null>(null);

  useEffect(() => {
    if (!req) return;
    const ctl = new AbortController();
    (async () => {
      try {
        const once = async () => {
          begin();
          try { return await streamAgent("simulate_buy", { params: req.params, signal: ctl.signal, onEvent: (e) => { if (!ctl.signal.aborted) onEvent(e); } }); }
          finally { finish(); }
        };
        let r = await once();
        if (!r) { await sleep(500, ctl.signal); r = await once(); }
        if (!r?.result.buy) throw new Error("no buy result");
        if (!ctl.signal.aborted) { setLast({ id: req.id, view: toBuyView(r.result.buy), params: req.params }); setFailedId(null); }
      } catch {
        if (!ctl.signal.aborted) setFailedId(req.id);
      }
    })();
    return () => ctl.abort();
  }, [req, begin, onEvent, finish]);

  const run = useCallback((params: BuyParams) => setReq((r) => ({ id: (r?.id ?? 0) + 1, params })), []);
  return {
    agent, view: last?.view ?? null, lastParams: last?.params ?? null, run,
    loading: !!req && last?.id !== req.id && failedId !== req.id,
    failed: !!req && failedId === req.id,
  };
}
