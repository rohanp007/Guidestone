"use client";
import { useMemo } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { LiquidForecast } from "@/lib/api";
import { compactMoney, dateLong, dateShort, isoOf, money0, parseDay } from "@/lib/format";
import { C } from "@/lib/tokens";
import { AXIS, DRAW_MS, niceTicks, Swatch, TooltipCard, usePrefersReducedMotion } from "./chartkit";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

const DAY = 86_400_000;
const HORIZON_DAYS = 180;
type Pt = { t: number; p10: number; p50: number; p90: number; band: number };

/** Daily points for the first 90 days, then the month-end points to the end of the 6-month horizon (day 181). One time axis, so the spacing is honest. */
export function toPoints(f: LiquidForecast): Pt[] {
  const t0 = parseDay(f.as_of_date).getTime();
  const pt = (t: number, p10: number, p50: number, p90: number): Pt => ({ t, p10, p50, p90, band: p90 - p10 });
  const pts: Pt[] = (f.daily?.p50 ?? []).map((_, i) => pt(t0 + i * DAY, f.daily!.p10[i], f.daily!.p50[i], f.daily!.p90[i]));
  const last = pts.length ? pts[pts.length - 1].t : t0;
  for (const m of f.monthly ?? []) {
    const t = parseDay(m.day).getTime();
    if (t > last && t <= t0 + (HORIZON_DAYS + 1) * DAY) pts.push(pt(t, m.p10, m.p50, m.p90));
  }
  return pts;
}

function summary(f: LiquidForecast, pts: Pt[]): string {
  const end = pts[pts.length - 1];
  const low = f.low_point;
  const c = f.cushion_risk;
  let s = `Median cash goes from ${money0(f.start_liquid)} today to ${money0(end.p50)} on ${dateLong(isoOf(end.t))}. The lowest median balance is ${money0(low.p50)} on ${dateLong(low.day)}. `;
  s += `The cushion is ${money0(c.buffer)} (one month of essentials). `;
  s += c.first_date_p50 ? `The median path is below it from ${dateLong(c.first_date_p50)}.` : "The median path stays above it.";
  return s;
}

export default function CashForecast({ load }: { load: Load<LiquidForecast> }) {
  const reduce = usePrefersReducedMotion();
  const f = load.data;
  const pts = useMemo(() => (f ? toPoints(f) : []), [f]);
  const events = useMemo(() => (f ? f.events.filter((e) => { const t = parseDay(e.day).getTime(); return pts.length && t > pts[0].t && t <= pts[pts.length - 1].t; }) : []), [f, pts]);
  const cushion = f?.cushion_risk.buffer ?? 0;
  const ticks = useMemo(() => (pts.length ? niceTicks(Math.min(0, cushion, ...pts.map((p) => p.p10)), Math.max(cushion, ...pts.map((p) => p.p90))) : []), [pts, cushion]);
  const xTicks = useMemo(() => {
    if (!pts.length) return [];
    const out: number[] = [];
    const a = new Date(pts[0].t), end = pts[pts.length - 1].t;
    for (let d = new Date(a.getFullYear(), a.getMonth() + 1, 1); d.getTime() <= end; d = new Date(d.getFullYear(), d.getMonth() + 1, 1)) out.push(d.getTime());
    return out;
  }, [pts]);
  const yAt = (t: number) => pts.reduce((best, p) => (Math.abs(p.t - t) < Math.abs(best.t - t) ? p : best), pts[0]).p50;
  const eventsFor = (t: number) => {
    const i = pts.findIndex((p) => p.t === t);
    const prev = i > 0 ? pts[i - 1].t : -Infinity;
    return events.filter((e) => { const et = parseDay(e.day).getTime(); return et > prev && et <= t; });
  };

  return (
    <Section id="forecast-h" title="Cash forecast" aside="Next 180 days">
      {load.error && <Unavailable>The cash forecast is unavailable right now.</Unavailable>}
      {!f && !load.error && <Skeleton className="h-[280px] w-full" />}
      {f && pts.length > 0 && (
        <figure>
          <figcaption className="text-[15px] text-ink-2">{summary(f, pts)}</figcaption>
          <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 text-[13px] text-ink-2">
            <span className="inline-flex items-center gap-2"><Swatch kind="line" /> Median</span>
            <span className="inline-flex items-center gap-2"><Swatch kind="band" /> Likely range (10th–90th)</span>
            <span className="inline-flex items-center gap-2"><Swatch kind="dash" /> Cushion</span>
            <span className="inline-flex items-center gap-2"><Swatch kind="zero" /> $0</span>
            {events.length > 0 && <span className="inline-flex items-center gap-2"><Swatch kind="dot" /> Event</span>}
          </div>
          <div className="mt-3 h-[260px] w-full sm:h-[300px]" role="img" aria-label={summary(f, pts)}>
            <ResponsiveContainer>
              <ComposedChart data={pts} margin={{ top: 8, right: 16, left: 0, bottom: 0 }}>
                <CartesianGrid vertical={false} stroke={C.line} />
                <XAxis dataKey="t" type="number" scale="time" domain={[pts[0].t, pts[pts.length - 1].t]} ticks={xTicks}
                  tickFormatter={(t: number) => new Intl.DateTimeFormat("en-US", { month: "short" }).format(t)} tick={AXIS} tickLine={false} axisLine={{ stroke: C.lineStrong }} tickMargin={6} />
                <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} ticks={ticks} domain={[ticks[0], ticks[ticks.length - 1]]} interval={0} />
                <Tooltip cursor={{ stroke: C.ink3, strokeWidth: 1 }} isAnimationActive={false}
                  content={({ active, payload }) => {
                    const p: Pt | undefined = active ? payload?.[0]?.payload : undefined;
                    if (!p) return null;
                    return (
                      <TooltipCard>
                        <div className="label !text-[11px]">{dateLong(isoOf(p.t))}</div>
                        <div className="num mt-1 text-[16px] font-medium">{money0(p.p50)}</div>
                        <div className="num text-ink-2">{money0(p.p10)} – {money0(p.p90)}</div>
                        {eventsFor(p.t).map((e, i) => <div key={i} className="mt-1.5 max-w-[240px] text-ink-2">{e.description}</div>)}
                      </TooltipCard>
                    );
                  }} />
                <Area dataKey="p10" stackId="band" stroke="none" fill="transparent" isAnimationActive={false} activeDot={false} />
                <Area dataKey="band" stackId="band" stroke="none" fill={C.navyTint} fillOpacity={1} activeDot={false} isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
                <ReferenceLine y={0} stroke={C.ink} strokeWidth={1} />
                <ReferenceLine y={cushion} stroke={C.ink2} strokeDasharray="4 4" />
                <Line dataKey="p50" stroke={C.navy} strokeWidth={2} dot={false} activeDot={{ r: 4, fill: C.navy, stroke: C.surface, strokeWidth: 2 }}
                  isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
                {events.map((e) => { const t = parseDay(e.day).getTime(); return <ReferenceDot key={`${e.kind}-${e.day}`} x={t} y={yAt(t)} r={5} fill={C.orange} stroke={C.surface} strokeWidth={2} ifOverflow="visible" />; })}
              </ComposedChart>
            </ResponsiveContainer>
          </div>
          {events.length > 0 && (
            <ul className="mt-4 space-y-1.5 text-[14px]">
              {events.map((e) => (
                <li key={`${e.kind}-${e.day}`} className="grid grid-cols-[5.5rem_1fr] gap-x-3"><span className="num text-ink-2">{dateShort(e.day)}</span><span>{e.description}</span></li>
              ))}
            </ul>
          )}
          <p className="mt-4 text-[13px] text-ink-2">
            The range is a simulation, not a promise. On customers the model had never seen, the 80% band contained the actual outcome 82% of the time at month-end (84% of days).
          </p>
          <details className="mt-3 text-[13px] text-ink-2">
            <summary className="cursor-pointer select-none">View as table</summary>
            <div className="mt-2 max-h-64 overflow-auto">
              <table className="num w-full text-left">
                <thead><tr><th className="py-1 pr-4 font-medium">Date</th><th className="pr-4 text-right font-medium">10th</th><th className="pr-4 text-right font-medium">Median</th><th className="text-right font-medium">90th</th></tr></thead>
                <tbody>{pts.filter((_, i) => i % 7 === 0 || i === pts.length - 1).map((p) => (
                  <tr key={p.t} className="border-t border-line"><td className="py-1 pr-4">{dateShort(isoOf(p.t))}</td><td className="pr-4 text-right">{money0(p.p10)}</td><td className="pr-4 text-right">{money0(p.p50)}</td><td className="text-right">{money0(p.p90)}</td></tr>))}</tbody>
              </table>
            </div>
          </details>
        </figure>
      )}
    </Section>
  );
}
