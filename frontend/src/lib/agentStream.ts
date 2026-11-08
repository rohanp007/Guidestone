/**
 * Run an orchestrator event over the stream endpoint: POST, then read the response body as server-sent events with a ReadableStream
 * reader (EventSource cannot POST). The final "result" event replaces the normal fetch, so it is one request. If the connection fails
 * before the result arrives, fall back to the plain endpoint.
 */
import { ApiError, runAgent, type AnalysisResult } from "./api";
import type { StreamEvent } from "./agentActivity";

export type AgentRun = { result: AnalysisResult; cached: boolean };

/** Split an SSE buffer into complete events. Returns the parsed events and what is left over. */
export function parseSse(buffer: string): { events: StreamEvent[]; rest: string } {
  const blocks = buffer.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: StreamEvent[] = [];
  for (const block of blocks) {
    const data = block.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trimStart()).join("\n");
    if (!data) continue;
    try { events.push(JSON.parse(data) as StreamEvent); } catch { /* a malformed event is skipped */ }
  }
  return { events, rest };
}

/** Like runAgent: null when a newer run replaced this one (the caller asks again). `onEvent` sees every event, including the last. */
export async function streamAgent(event: string, opts: { refresh?: boolean; params?: Record<string, unknown>; signal?: AbortSignal; onEvent?: (e: StreamEvent) => void } = {}): Promise<AgentRun | null> {
  const plain = () => runAgent(event, { refresh: opts.refresh, params: opts.params, signal: opts.signal });
  let res: Response;
  try {
    res = await fetch(`/api/me/agent/${encodeURIComponent(event)}/stream${opts.refresh ? "?refresh=1" : ""}`, {
      method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(opts.params ?? {}), signal: opts.signal,
    });
  } catch (e) {
    if (opts.signal?.aborted) throw e;
    return plain();
  }
  if (!res.ok || !res.body) return plain();          // 401 / 422 / 5xx: the plain endpoint reports them the usual way (and redirects on 401)
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let first: StreamEvent | null = null;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, "\n");
      const parsed = parseSse(buffer);
      buffer = parsed.rest;
      for (const ev of parsed.events) {
        first ??= ev;
        opts.onEvent?.(ev);
        if (ev.type === "result" && ev.result) return { result: ev.result, cached: first.detail?.cached === true };
        if (ev.type === "error") {
          if (ev.label === "A newer check-up replaced this one") return null;
          throw new ApiError(500, "the check-up could not be completed");
        }
      }
    }
  } catch (e) {
    if (opts.signal?.aborted || e instanceof ApiError) throw e;
  } finally {
    reader.cancel().catch(() => {});
  }
  return plain();                                    // the stream ended without a result: ask the plain endpoint (it joins the run)
}
