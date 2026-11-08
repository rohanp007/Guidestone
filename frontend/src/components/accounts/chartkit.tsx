"use client";
import { useSyncExternalStore, type ReactNode } from "react";
import { C } from "@/lib/tokens";

export const AXIS = { fontSize: 12, fontFamily: "var(--font-plex-mono), ui-monospace, monospace", fill: C.ink3 } as const;
export const DRAW_MS = 500;

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

export function ChartCard({ title, note, right, legend, children, table }: {
  title: string; note?: string; right?: ReactNode; legend?: ReactNode; children: ReactNode; table?: ReactNode;
}) {
  return (
    <section className="rounded-[6px] border border-line bg-surface p-4 sm:p-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="text-[20px] font-semibold">{title}</h2>
          {note && <p className="mt-1 text-[14px] text-ink-2">{note}</p>}
        </div>
        {right}
      </div>
      {legend && <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-ink-2">{legend}</div>}
      <div className="mt-4 h-[260px] w-full sm:h-[320px]">{children}</div>
      {table && (
        <details className="mt-3 text-[13px] text-ink-2">
          <summary className="cursor-pointer select-none">View as table</summary>
          <div className="mt-2 max-h-64 overflow-auto">{table}</div>
        </details>
      )}
    </section>
  );
}

export const Swatch = ({ kind }: { kind: "line" | "band" | "dash" | "dot" | "area" }) => {
  const s = { line: <span className="h-0.5 w-5 rounded bg-navy" />, band: <span className="h-3 w-5 rounded-[2px] bg-navy-tint border border-line-strong" />,
    dash: <span className="w-5 border-t-2 border-dashed border-ink-3" />, dot: <span className="h-2.5 w-2.5 rounded-full bg-orange" />,
    area: <span className="h-3 w-5 rounded-[2px] bg-navy-tint border-t-2 border-navy" /> }[kind];
  return <span className="inline-flex items-center">{s}</span>;
};

/** Segmented control (pill) used for the monthly / daily toggle. */
export function Segmented<T extends string>({ value, options, onChange, label }: { value: T; options: T[]; onChange: (v: T) => void; label: string }) {
  return (
    <div role="group" aria-label={label} className="inline-flex rounded-full border border-line-strong p-0.5 text-[13px]">
      {options.map((o) => (
        <button key={o} aria-pressed={o === value} onClick={() => onChange(o)}
          className={`press rounded-full px-3 py-1 capitalize ${o === value ? "bg-navy text-white" : "text-ink-2"}`}>{o}</button>
      ))}
    </div>
  );
}

export const TooltipCard = ({ children }: { children: ReactNode }) => (
  <div className="rounded-[6px] border border-line-strong bg-surface px-3 py-2 text-[13px] shadow-[0_4px_14px_rgba(14,27,44,0.08)]">{children}</div>
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
