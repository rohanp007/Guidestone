"use client";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { IDLE, reduce, type AgentName, type StreamEvent } from "./agentActivity";

/** Live state for one AgentActivity window. `begin` when a request starts, `onEvent` for every stream event, `finish` when it ends.
 *  Wraps the pure reducer in agentActivity.ts and adds what the reducer does not keep: the label of the step an agent is working on now. */
export function useAgentActivity() {
  const [activity, dispatch] = useReducer(reduce, IDLE);
  const [now, setNow] = useState(0);
  const [current, setCurrent] = useState<Partial<Record<AgentName, string>>>({});
  const [latest, setLatest] = useState("");
  const saw = useRef(false);

  const begin = useCallback(() => {
    saw.current = false;
    setCurrent({});
    setLatest("");
    dispatch({ type: "begin", now: performance.now() });
  }, []);
  const onEvent = useCallback((ev: StreamEvent) => {
    saw.current = true;
    const t = performance.now();
    dispatch({ type: "event", ev, now: t });
    setNow(t);
    if (ev.type === "progress" && ev.agent && ev.label) {
      const agent = ev.agent as AgentName;
      setCurrent((c) => { const n = { ...c }; if (ev.phase === "start") n[agent] = ev.label as string; else delete n[agent]; return n; });
      setLatest(ev.label);
    } else if (ev.type === "result") setCurrent({});
  }, []);
  /** The request ended. If no stream event ever arrived (the plain endpoint answered instead), there is nothing to show. */
  const finish = useCallback(() => { if (!saw.current) dispatch({ type: "reset" }); }, []);

  useEffect(() => {
    if (activity.phase !== "running") return;
    const id = setInterval(() => setNow(performance.now()), 100);
    return () => clearInterval(id);
  }, [activity.phase]);

  return { activity, current, latest, now, begin, onEvent, finish };
}
export type AgentActivityHandle = ReturnType<typeof useAgentActivity>;
