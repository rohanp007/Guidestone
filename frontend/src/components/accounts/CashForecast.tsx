"use client";
import { useMemo } from "react";
import type { LiquidForecast } from "@/lib/api";
import { dateLong, dateShort, isoOf, money0, parseDay } from "@/lib/format";
import ForecastBand, { type BandPoint } from "@/components/charts/ForecastBand";
import { Section, Skeleton, Unavailable, type Load } from "./ui";

const DAY = 86_400_000;
const HORIZON_DAYS = 180;
type Pt = BandPoint & { band: number };

/** One daily point per day over the whole horizon (the API sends a daily band for every day), so the resolution never changes. */
export function toPoints(f: LiquidForecast): Pt[] {
  const t0 = parseDay(f.as_of_date).getTime();
  const d = f.daily;
  if (!d) return [];
  return d.p50.map((_, i) => ({ t: t0 + i * DAY, p10: d.p10[i], p50: d.p50[i], p90: d.p90[i], band: d.p90[i] - d.p10[i] })).filter((p) => p.t <= t0 + (HORIZON_DAYS + 1) * DAY);
}

/** Runs of consecutive days where the median path is under the cushion, as [first day, last day] (ms). */
export function belowRuns(pts: BandPoint[], cushion: number): [number, number][] {
  const runs: [number, number][] = [];
  for (const p of pts) {
    if (p.p50 >= cushion) continue;
    const last = runs[runs.length - 1];
    if (last && p.t - last[1] <= DAY) last[1] = p.t; else runs.push([p.t, p.t]);
  }
  return runs;
}

const rangeLabel = ([a, b]: [number, number]) => (a === b ? dateShort(isoOf(a)) : `${dateShort(isoOf(a))} – ${dateShort(isoOf(b))}`);

function summary(f: LiquidForecast, pts: Pt[]): string {
  const end = pts[pts.length - 1];
  const low = f.low_point;
  const cushion = f.cushion_risk.buffer;
  const runs = belowRuns(pts, cushion);
  let s = `Median cash goes from ${money0(f.start_liquid)} today to ${money0(end.p50)} on ${dateLong(isoOf(end.t))}. The lowest median balance is ${money0(low.p50)} on ${dateLong(low.day)}. `;
  s += `The 1-month cushion is ${money0(cushion)}. `;
  s += runs.length ? `The median path is below it ${runs.map(rangeLabel).join(", ")}.` : "The median path stays above it.";
  return s;
}

export default function CashForecast({ load }: { load: Load<LiquidForecast> }) {
  const f = load.data;
  const pts = useMemo(() => (f ? toPoints(f) : []), [f]);
  const events = useMemo(() => (f ? f.events.filter((e) => { const t = parseDay(e.day).getTime(); return pts.length && t > pts[0].t && t <= pts[pts.length - 1].t; }) : []), [f, pts]);
  const series = useMemo(() => [{ id: "cash", label: "Median", points: pts, tone: "primary" as const, band: true }], [pts]);
  const markers = useMemo(() => events.map((e) => ({ t: parseDay(e.day).getTime(), description: e.description })), [events]);

  return (
    <Section id="forecast-h" title="Cash forecast" aside="Next 180 days">
      {load.error && <Unavailable>The cash forecast is unavailable right now.</Unavailable>}
      {!f && !load.error && <Skeleton className="h-[280px] w-full" />}
      {f && pts.length > 0 && (
        <ForecastBand series={series} summary={summary(f, pts)} summaryText={summary(f, pts)} markers={markers} tableStepDays={7}
          refLines={[{ y: f.cushion_risk.buffer, label: "1-month cushion", style: "dash" }, { y: 0, label: "$0", style: "zero" }]}>
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
        </ForecastBand>
      )}
    </Section>
  );
}
