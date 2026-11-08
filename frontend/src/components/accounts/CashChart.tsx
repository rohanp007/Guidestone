"use client";
import { useMemo, useState } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { LiquidForecast } from "@/lib/api";
import { compactMoney, dateShort, money0, monthFull, monthLabel } from "@/lib/format";
import { C } from "@/lib/tokens";
import { AXIS, ChartCard, DRAW_MS, niceTicks, usePrefersReducedMotion, Segmented, Swatch, TooltipCard } from "./chartkit";

type Pt = { x: string; label: string; p10: number; p50: number; p90: number; band: number };
type Ev = LiquidForecast["events"][number];

function toPoints(f: LiquidForecast, view: "monthly" | "daily"): { pts: Pt[]; key: (day: string) => string } {
  if (view === "daily" && f.daily) {
    const start = new Date(f.as_of_date + "T00:00:00");
    return {
      pts: f.daily.p50.map((_, i) => {
        const d = new Date(start);
        d.setDate(d.getDate() + i);
        const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
        return { x: iso, label: dateShort(iso), p10: f.daily!.p10[i], p50: f.daily!.p50[i], p90: f.daily!.p90[i], band: f.daily!.p90[i] - f.daily!.p10[i] };
      }),
      key: (day) => day,
    };
  }
  return {
    pts: (f.monthly ?? []).map((m) => ({ x: m.month, label: monthLabel(m.month), p10: m.p10, p50: m.p50, p90: m.p90, band: m.p90 - m.p10 })),
    key: (day) => day.slice(0, 7),
  };
}

export default function CashChart({ forecast }: { forecast: LiquidForecast }) {
  const reduce = usePrefersReducedMotion();
  const [view, setView] = useState<"monthly" | "daily">("monthly");
  const { pts, key } = useMemo(() => toPoints(forecast, view), [forecast, view]);
  const cushion = forecast.cushion_risk.buffer;
  const ticks = useMemo(() => niceTicks(Math.min(0, cushion, ...pts.map((p) => p.p10)), Math.max(...pts.map((p) => p.p90))), [pts, cushion]);
  const events = useMemo(() => {
    const by = new Map<string, Ev[]>();
    for (const e of forecast.events) by.set(key(e.day), [...(by.get(key(e.day)) ?? []), e]);
    return by;
  }, [forecast, key]);

  return (
    <ChartCard
      title="Cash forecast"
      note={`Liquid cash over the next ${forecast.horizon_months} months. The band is the range 80% of outcomes fall in.`}
      right={<Segmented label="Resolution" value={view} options={["monthly", "daily"]} onChange={setView} />}
      legend={<>
        <span className="inline-flex items-center gap-2"><Swatch kind="line" /> Median</span>
        <span className="inline-flex items-center gap-2"><Swatch kind="band" /> 10th–90th percentile</span>
        <span className="inline-flex items-center gap-2"><Swatch kind="dash" /> Cushion (one month of essentials)</span>
        <span className="inline-flex items-center gap-2"><Swatch kind="dot" /> Event</span>
      </>}
      table={
        <table className="w-full text-left mono">
          <thead><tr><th className="py-1 pr-4">{view === "monthly" ? "Month" : "Day"}</th><th className="pr-4">10th</th><th className="pr-4">Median</th><th>90th</th></tr></thead>
          <tbody>{pts.map((p) => <tr key={p.x} className="border-t border-line"><td className="py-1 pr-4">{p.label}</td><td className="pr-4">{money0(p.p10)}</td><td className="pr-4">{money0(p.p50)}</td><td>{money0(p.p90)}</td></tr>)}</tbody>
        </table>
      }
    >
      <ResponsiveContainer>
        <ComposedChart data={pts} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={C.line} strokeDasharray="0" />
          <XAxis dataKey="label" padding={{ left: 16, right: 0 }} tickMargin={6} tick={AXIS} tickLine={false} axisLine={{ stroke: C.lineStrong }} minTickGap={28} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} ticks={ticks} domain={[ticks[0], ticks[ticks.length - 1]]} interval={0} />
          <Tooltip
            cursor={{ stroke: C.ink3, strokeWidth: 1 }}
            isAnimationActive={false}
            content={({ active, payload }) => {
              const p: Pt | undefined = active ? payload?.[0]?.payload : undefined;
              if (!p) return null;
              const ev = events.get(p.x) ?? [];
              return (
                <TooltipCard>
                  <div className="label !text-[11px]">{view === "monthly" ? monthFull(p.x) : p.label}</div>
                  <div className="mt-1 text-[16px] font-medium">{money0(p.p50)}</div>
                  <div className="mono text-ink-2">{money0(p.p10)} – {money0(p.p90)}</div>
                  {ev.map((e, i) => (
                    <div key={i} className="mt-1.5 flex max-w-[260px] items-start gap-2 text-ink-2">
                      <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-orange" />
                      <span>{e.description}</span>
                    </div>
                  ))}
                </TooltipCard>
              );
            }}
          />
          <Area dataKey="p10" stackId="band" stroke="none" fill="transparent" isAnimationActive={false} activeDot={false} />
          <Area dataKey="band" stackId="band" stroke="none" fill={C.navyTint} fillOpacity={1} activeDot={false}
            isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
          <Line dataKey="p50" stroke={C.navy} strokeWidth={2} dot={false} activeDot={{ r: 4, fill: C.navy, stroke: C.surface, strokeWidth: 2 }}
            isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
          <ReferenceLine y={cushion} stroke={C.ink3} strokeDasharray="4 4"
            label={{ value: `Cushion ${money0(cushion)}`, position: "insideBottomRight", fill: C.ink3, fontSize: 12, fontFamily: AXIS.fontFamily }} />
          {[...events.keys()].map((k) => {
            const pt = pts.find((p) => p.x === k);
            return pt ? <ReferenceDot key={k} x={pt.label} y={pt.p50} r={5} fill={C.orange} stroke={C.surface} strokeWidth={2} ifOverflow="visible" /> : null;
          })}
        </ComposedChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}
