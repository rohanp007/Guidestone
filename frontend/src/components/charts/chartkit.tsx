"use client";
import { useSyncExternalStore, type ReactNode } from "react";
import { C } from "@/lib/tokens";

export const AXIS = { fontSize: 12, fontFamily: "var(--font-plex-mono), ui-monospace, monospace", fill: C.ink2 } as const;
export const DRAW_MS = 400;

/** Evenly spaced, round tick values (1, 2, 2.5, 5 x 10^n) covering [min, max]; the axis domain should be the first and last tick. */
export function niceTicks(min: number, max: number, target = 5): number[] {
  if (!Number.isFinite(min) || !Number.isFinite(max)) return [0, 1];
  if (min === max) max = min + 1;
  const raw = (max - min) / (target - 1);
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? 10 * mag;
  const out: number[] = [];
  for (let v = Math.floor(min / step) * step; v <= Math.ceil(max / step) * step + step / 1e6; v += step) out.push(+v.toFixed(6));
  return out;
}

export const Swatch = ({ kind }: { kind: "line" | "mutedline" | "band" | "dash" | "dot" | "zero" | "shade" | "dotted" }) => {
  const s = {
    line: <span className="h-0.5 w-5 rounded bg-navy" />,
    mutedline: <span className="w-5 border-t-2 border-dashed border-ink-3" />,
    shade: <span className="h-3 w-5 rounded-[2px] bg-orange-tint" />,
    band: <span className="h-3 w-5 rounded-[2px] border border-line-strong bg-navy-tint" />,
    dash: <span className="w-5 border-t-2 border-dashed border-ink-2" />,
    dotted: <span className="w-5 border-t-2 border-dotted border-ink-2" />,
    zero: <span className="h-px w-5 bg-ink" />,
    dot: <span className="h-2.5 w-2.5 rounded-full bg-orange" />,
  }[kind];
  return <span className="inline-flex items-center" aria-hidden>{s}</span>;
};

/** Tooltip box: hairline border, no shadow. */
export const TooltipCard = ({ children }: { children: ReactNode }) => (
  <div className="rounded-[6px] border border-line-strong bg-surface px-3 py-2 text-[13px]">{children}</div>
);

const QUERY = "(prefers-reduced-motion: reduce)";
/** Recharts draws with JS, so it does not see the CSS reduced-motion rule; ask the media query directly. */
export function usePrefersReducedMotion() {
  return useSyncExternalStore(
    (cb) => { const m = window.matchMedia(QUERY); m.addEventListener("change", cb); return () => m.removeEventListener("change", cb); },
    () => window.matchMedia(QUERY).matches,
    () => false,
  );
}
