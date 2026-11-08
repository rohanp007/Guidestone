"use client";
import { useMemo, type ReactNode } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceArea, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { compactMoney, dateLong, dateShort, isoOf, money0 } from "@/lib/format";
import { C } from "@/lib/tokens";
import { AXIS, DRAW_MS, niceTicks, Swatch, TooltipCard, usePrefersReducedMotion } from "./chartkit";

export type BandPoint = { t: number; p10: number; p50: number; p90: number };
/** One median line with an optional p10–p90 band. `primary` is navy and emphasised; `muted` is grey. */
export type BandSeries = { id: string; label: string; points: BandPoint[]; tone: "primary" | "muted"; band?: boolean };
export type RefLine = { y: number; label: string; style: "dash" | "dotted" | "zero" };
export type ShadedRange = { x1: number; x2: number; label: string };
export type ChartMarker = { t: number; description: string };

type Props = {
  series: BandSeries[];
  refLines?: RefLine[];
  shadedRanges?: ShadedRange[];
  markers?: ChartMarker[];
  /** Text summary of the chart (also its accessible name). */
  summary: ReactNode;
  summaryText: string;
  /** Extra content under the chart (event list, caption). */
  children?: ReactNode;
  heightClass?: string;
  legendExtra?: ReactNode;
  tableStepDays?: number;
};

const NAVY = { line: C.navy, fill: C.navyTint };
const GREY = { line: C.ink3, fill: "#EEEBE4" };

/** Median lines with likely-range bands over a time axis, with reference lines, shaded periods and event markers.
 *  Data-driven: no fetching. Used by the accounts forecast and the Pay Off cash chart. */
export default function ForecastBand({ series, refLines = [], shadedRanges = [], markers = [], summary, summaryText, children, heightClass = "h-[260px] sm:h-[300px]", legendExtra, tableStepDays = 1 }: Props) {
  const reduce = usePrefersReducedMotion();
  const { data, tMin, tMax } = useMemo(() => {
    const by = new Map<number, Record<string, number>>();
    for (const s of series) for (const p of s.points) {
      const row = by.get(p.t) ?? { t: p.t };
      row[`${s.id}_p10`] = p.p10; row[`${s.id}_p50`] = p.p50; row[`${s.id}_p90`] = p.p90; row[`${s.id}_band`] = p.p90 - p.p10;
      by.set(p.t, row);
    }
    const rows = [...by.values()].sort((a, b) => a.t - b.t);
    return { data: rows, tMin: rows[0]?.t ?? 0, tMax: rows[rows.length - 1]?.t ?? 1 };
  }, [series]);

  const ticks = useMemo(() => {
    const all = series.flatMap((s) => s.points.flatMap((p) => [p.p10, p.p90]));
    return niceTicks(Math.min(0, ...refLines.map((r) => r.y), ...all), Math.max(...refLines.map((r) => r.y), ...all));
  }, [series, refLines]);
  const xTicks = useMemo(() => {
    const out: number[] = [];
    const a = new Date(tMin);
    for (let d = new Date(a.getFullYear(), a.getMonth() + 1, 1); d.getTime() <= tMax; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) out.push(d.getTime());
    return out.length ? out : [tMin];
  }, [tMin, tMax]);
  const ts = data.map((r) => r.t);
  const markersFor = (t: number) => {
    const i = ts.indexOf(t);
    const prev = i > 0 ? ts[i - 1] : -Infinity;
    return markers.filter((m) => m.t > prev && m.t <= t);
  };
  const yAt = (t: number) => {
    const s = series[0];
    return s.points.reduce((best, p) => (Math.abs(p.t - t) < Math.abs(best.t - t) ? p : best), s.points[0]).p50;
  };
  if (!data.length) return null;
  const primary = series.filter((s) => s.tone === "primary"), muted = series.filter((s) => s.tone === "muted");
  const bandAreas = [...muted, ...primary].flatMap((s) => (s.band ? [
    <Area key={`${s.id}-lo`} dataKey={`${s.id}_p10`} stackId={s.id} stroke="none" fill="transparent" isAnimationActive={false} activeDot={false} connectNulls />,
    <Area key={`${s.id}-band`} dataKey={`${s.id}_band`} stackId={s.id} stroke="none" fill={(s.tone === "primary" ? NAVY : GREY).fill} fillOpacity={s.tone === "primary" ? 1 : 0.7} activeDot={false} connectNulls
      isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />,
  ] : []));

  return (
    <figure>
      <figcaption className="text-[15px] text-ink-2">{summary}</figcaption>
      <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-ink-2">
        {series.map((s) => <span key={s.id} className="inline-flex items-center gap-2">{s.tone === "primary" ? <Swatch kind="line" /> : <Swatch kind="mutedline" />}{s.label}</span>)}
        {series.some((s) => s.band) && <span className="inline-flex items-center gap-2"><Swatch kind="band" /> Likely range (10th–90th)</span>}
        {refLines.map((r) => <span key={r.label} className="inline-flex items-center gap-2"><Swatch kind={r.style === "zero" ? "zero" : r.style === "dotted" ? "dotted" : "dash"} />{r.label}</span>)}
        {shadedRanges.length > 0 && <span className="inline-flex items-center gap-2"><Swatch kind="shade" /> {shadedRanges[0].label}</span>}
        {markers.length > 0 && <span className="inline-flex items-center gap-2"><Swatch kind="dot" /> Event</span>}
        {legendExtra}
      </div>
      <div className={`mt-3 w-full ${heightClass}`} role="img" aria-label={summaryText}>
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
            <CartesianGrid vertical={false} stroke={C.line} />
            <XAxis dataKey="t" type="number" scale="time" domain={[tMin, tMax]} ticks={xTicks} tickFormatter={(t: number) => new Intl.DateTimeFormat("en-US", { month: "short" }).format(t)}
              tick={AXIS} tickLine={false} axisLine={{ stroke: C.lineStrong }} tickMargin={6} />
            <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} ticks={ticks} domain={[ticks[0], ticks[ticks.length - 1]]} interval={0} />
            {shadedRanges.map((r, i) => <ReferenceArea key={i} x1={r.x1} x2={r.x2} fill={C.orangeTint} fillOpacity={0.9} stroke="none" ifOverflow="hidden" />)}
            <Tooltip cursor={{ stroke: C.ink3, strokeWidth: 1 }} isAnimationActive={false}
              content={({ active, payload }) => {
                const row = active ? (payload?.[0]?.payload as Record<string, number> | undefined) : undefined;
                if (!row) return null;
                return (
                  <TooltipCard>
                    <div className="label !text-[11px]">{dateLong(isoOf(row.t))}</div>
                    {series.map((s) => row[`${s.id}_p50`] === undefined ? null : (
                      <div key={s.id} className="mt-1">
                        {series.length > 1 && <div className="text-[12px] text-ink-2">{s.label}</div>}
                        <div className="num text-[16px] font-medium">{money0(row[`${s.id}_p50`])}</div>
                        {s.band && <div className="num text-ink-2">{money0(row[`${s.id}_p10`])} – {money0(row[`${s.id}_p90`])}</div>}
                      </div>
                    ))}
                    {markersFor(row.t).map((m, i) => <div key={i} className="mt-1.5 max-w-[240px] text-ink-2">{m.description}</div>)}
                  </TooltipCard>
                );
              }} />
            {bandAreas}
            {refLines.map((r) => r.style === "zero"
              ? <ReferenceLine key={r.label} y={r.y} stroke={C.ink} strokeWidth={1} />
              : <ReferenceLine key={r.label} y={r.y} stroke={C.ink2} strokeDasharray={r.style === "dotted" ? "1.5 3.5" : "5 4"} label={{ value: r.label, position: "insideBottomRight", fill: C.ink2, fontSize: 12, fontFamily: AXIS.fontFamily }} />)}
            {muted.map((s) => <Line key={s.id} dataKey={`${s.id}_p50`} stroke={C.ink3} strokeWidth={1.5} strokeDasharray="5 4" dot={false} activeDot={false} connectNulls isAnimationActive={false} />)}
            {primary.map((s) => <Line key={s.id} dataKey={`${s.id}_p50`} stroke={C.navy} strokeWidth={2} dot={false} activeDot={{ r: 4, fill: C.navy, stroke: C.surface, strokeWidth: 2 }} connectNulls
              isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />)}
            {markers.map((m) => <ReferenceDot key={`${m.t}-${m.description}`} x={m.t} y={yAt(m.t)} r={5} fill={C.orange} stroke={C.surface} strokeWidth={2} ifOverflow="visible" />)}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
      {children}
      <details className="mt-3 text-[13px] text-ink-2">
        <summary className="cursor-pointer select-none">View as table</summary>
        <div className="mt-2 max-h-64 overflow-auto">
          <table className="num w-full text-left">
            <thead><tr><th className="py-1 pr-4 font-medium">Date</th>{series.map((s) => <th key={s.id} className="pr-4 text-right font-medium">{series.length > 1 ? `${s.label} ` : ""}median{s.band ? " (10th–90th)" : ""}</th>)}</tr></thead>
            <tbody>{data.filter((_, i) => i % tableStepDays === 0 || i === data.length - 1).map((r) => (
              <tr key={r.t} className="border-t border-line"><td className="py-1 pr-4">{dateShort(isoOf(r.t))}</td>
                {series.map((s) => <td key={s.id} className="pr-4 text-right">{r[`${s.id}_p50`] === undefined ? "–" : money0(r[`${s.id}_p50`])}{s.band && r[`${s.id}_p10`] !== undefined ? ` (${money0(r[`${s.id}_p10`])} – ${money0(r[`${s.id}_p90`])})` : ""}</td>)}</tr>))}</tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
