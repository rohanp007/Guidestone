"use client";
import { useMemo, type ReactNode } from "react";
import { CartesianGrid, ComposedChart, Line, ReferenceDot, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { compactMoney, dateLong, dateShort, isoOf, money0 } from "@/lib/format";
import { C } from "@/lib/tokens";
import { AXIS, DRAW_MS, niceTicks, Swatch, TooltipCard, usePrefersReducedMotion } from "./chartkit";

export type TimelinePoint = { t: number; v: number };
export type TimelineMarker = { t: number; label: string };
type Props = {
  /** Total debt by month, keyed by series id (a strategy code). */
  seriesByStrategy: Record<string, TimelinePoint[]>;
  labels: Record<string, string>;
  emphasized: string;
  markers?: TimelineMarker[];
  summary: ReactNode;
  summaryText: string;
  children?: ReactNode;
};

/** Total debt over time: the emphasised series in navy, the others dashed grey references. Data-driven; no fetching. */
export default function BalanceTimeline({ seriesByStrategy, labels, emphasized, markers = [], summary, summaryText, children }: Props) {
  const reduce = usePrefersReducedMotion();
  const ids = Object.keys(seriesByStrategy);
  const { data, tMin, tMax } = useMemo(() => {
    const by = new Map<number, Record<string, number>>();
    for (const id of ids) for (const p of seriesByStrategy[id]) { const r = by.get(p.t) ?? { t: p.t }; r[id] = p.v; by.set(p.t, r); }
    const rows = [...by.values()].sort((a, b) => a.t - b.t);
    return { data: rows, tMin: rows[0]?.t ?? 0, tMax: rows[rows.length - 1]?.t ?? 1 };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- ids derives from seriesByStrategy
  }, [seriesByStrategy]);
  const ticks = useMemo(() => niceTicks(0, Math.max(1, ...data.flatMap((r) => ids.map((id) => r[id] ?? 0)))), [data, ids]);
  const xTicks = useMemo(() => {
    const spanMonths = Math.max(1, Math.round((tMax - tMin) / (30.4 * 86_400_000)));
    const step = Math.max(1, Math.ceil(spanMonths / 7));
    const out: number[] = [];
    const a = new Date(tMin);
    for (let i = 1, d = new Date(a.getFullYear(), a.getMonth() + 1, 1); d.getTime() <= tMax; i++, d = new Date(a.getFullYear(), a.getMonth() + 1 + i * step - step, 1)) out.push(d.getTime());
    return out.length ? out : [tMin];
  }, [tMin, tMax]);
  const fmt = useMemo(() => (tMax - tMin > 400 * 86_400_000 ? new Intl.DateTimeFormat("en-US", { month: "short", year: "2-digit" }) : new Intl.DateTimeFormat("en-US", { month: "short" })), [tMin, tMax]);
  if (!data.length) return null;
  const others = ids.filter((id) => id !== emphasized);
  const yAt = (t: number) => data.find((r) => r.t === t)?.[emphasized] ?? 0;

  return (
    <figure>
      <figcaption className="text-[15px] text-ink-2">{summary}</figcaption>
      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-ink-2">
        <span className="inline-flex items-center gap-2"><Swatch kind="line" />{labels[emphasized] ?? emphasized}</span>
        {others.map((id) => <span key={id} className="inline-flex items-center gap-2"><Swatch kind="mutedline" />{labels[id] ?? id}</span>)}
        {markers.length > 0 && <span className="inline-flex items-center gap-2"><Swatch kind="dot" /> Debt-free</span>}
      </div>
      <div className="mt-3 h-[240px] w-full sm:h-[280px]" role="img" aria-label={summaryText}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke={C.line} />
            <XAxis dataKey="t" type="number" scale="time" domain={[tMin, tMax]} ticks={xTicks} tickFormatter={(t: number) => fmt.format(t)} tick={AXIS} tickLine={false} axisLine={{ stroke: C.lineStrong }} tickMargin={6} />
            <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} ticks={ticks} domain={[0, ticks[ticks.length - 1]]} interval={0} />
            <Tooltip cursor={{ stroke: C.ink3, strokeWidth: 1 }} isAnimationActive={false}
              content={({ active, payload }) => {
                const row = active ? (payload?.[0]?.payload as Record<string, number> | undefined) : undefined;
                if (!row) return null;
                return (
                  <TooltipCard>
                    <div className="label !text-[11px]">{dateLong(isoOf(row.t))}</div>
                    {[emphasized, ...others].map((id) => row[id] === undefined ? null : (
                      <div key={id} className="mt-1"><div className="text-[12px] text-ink-2">{labels[id] ?? id}</div><div className="num text-[16px] font-medium">{money0(row[id])}</div></div>
                    ))}
                  </TooltipCard>
                );
              }} />
            {others.map((id) => <Line key={id} dataKey={id} stroke={C.ink3} strokeWidth={1.5} strokeDasharray="5 4" dot={false} activeDot={false} connectNulls isAnimationActive={false} />)}
            <Line dataKey={emphasized} stroke={C.navy} strokeWidth={2} dot={false} activeDot={{ r: 4, fill: C.navy, stroke: C.surface, strokeWidth: 2 }} connectNulls isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
            {markers.map((m) => <ReferenceDot key={m.t} x={m.t} y={yAt(m.t)} r={5} fill={C.orange} stroke={C.surface} strokeWidth={2} ifOverflow="visible" />)}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {children}
      <details className="mt-3 text-[13px] text-ink-2">
        <summary className="cursor-pointer select-none">View as table</summary>
        <div className="mt-2 max-h-64 overflow-auto">
          <table className="num w-full text-left">
            <thead><tr><th className="py-1 pr-4 font-medium">Month</th>{[emphasized, ...others].map((id) => <th key={id} className="pr-4 text-right font-medium">{labels[id] ?? id}</th>)}</tr></thead>
            <tbody>{data.filter((_, i) => data.length <= 24 || i % 3 === 0 || i === data.length - 1).map((r) => (
              <tr key={r.t} className="border-t border-line"><td className="py-1 pr-4">{dateShort(isoOf(r.t))} {new Date(r.t).getFullYear()}</td>
                {[emphasized, ...others].map((id) => <td key={id} className="pr-4 text-right">{r[id] === undefined ? "–" : money0(r[id])}</td>)}</tr>))}</tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
