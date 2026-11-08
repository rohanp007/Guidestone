"use client";
import { useState } from "react";
import { Area, ComposedChart, Line, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { LiquidForecast } from "@/lib/api";

type Pt = { x: string; p10: number; p50: number; p90: number; band: number };

const EVENT_COLOR: Record<string, string> = { extra_paycheck: "#16a34a", double_payment: "#dc2626" };
const DEFAULT_COLOR = "#f59e0b";
export const EVENT_LEGEND: [string, string][] = [
  ["extra_paycheck", "third paycheck"],
  ["double_payment", "payment lands twice"],
  ["other", "raise, renewal, loan ends, CD"],
];

/** Fan chart: p10-p90 band (stacked areas), p50 line, cushion line, event markers. Monthly view (month-end) or daily view (first 90 days). */
export default function ForecastChart({ f }: { f: LiquidForecast }) {
  const [view, setView] = useState<"monthly" | "daily">("monthly");
  const start = new Date(f.as_of_date + "T00:00:00");
  const daily = view === "daily" && f.daily;
  let data: Pt[];
  let xOf: (day: string) => string;
  if (daily) {
    data = f.daily!.p50.map((_, i) => {
      const d = new Date(start);
      d.setDate(d.getDate() + i + 1);
      const p10 = f.daily!.p10[i], p90 = f.daily!.p90[i];
      return { x: d.toISOString().slice(0, 10), p10, p50: f.daily!.p50[i], p90, band: p90 - p10 };
    });
    xOf = (day) => day;
  } else {
    data = (f.monthly ?? []).map((m) => ({ x: m.month, p10: m.p10, p50: m.p50, p90: m.p90, band: m.p90 - m.p10 }));
    xOf = (day) => day.slice(0, 7);
  }
  const cushion = f.cushion_risk.buffer;
  const pos = new Map(data.map((d) => [d.x, d]));
  const seen = new Set<string>();
  return (
    <div>
      <div className="flex gap-3 items-center text-xs mb-1">
        {(["monthly", "daily"] as const).map((v) => (
          <button key={v} onClick={() => setView(v)} className={`border px-2 py-0.5 ${view === v ? "bg-gray-900 text-white" : ""}`}>{v}</button>
        ))}
        {EVENT_LEGEND.map(([k, label]) => (
          <span key={k} className="flex items-center gap-1">
            <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: EVENT_COLOR[k] ?? DEFAULT_COLOR }} /> {label}
          </span>
        ))}
      </div>
      <div className="h-80 w-full">
        <ResponsiveContainer>
          <ComposedChart data={data} margin={{ left: 10, right: 10, top: 10 }}>
            <XAxis dataKey="x" minTickGap={30} tick={{ fontSize: 11 }} />
            <YAxis
              tickCount={7}
              tickFormatter={(v: number) => (Math.abs(v) >= 1000 ? `$${+(v / 1000).toFixed(1)}k` : `$${Math.round(v)}`)}
              tick={{ fontSize: 11 }}
              width={56}
            />
            <Tooltip formatter={(v, n) => (n === "band" ? null : [`$${Math.round(Number(v)).toLocaleString()}`, String(n)])} />
            <Area dataKey="p10" stackId="b" stroke="none" fill="transparent" isAnimationActive={false} name="p10" />
            <Area dataKey="band" stackId="b" stroke="none" fill="#3b82f6" fillOpacity={0.2} isAnimationActive={false} name="band" />
            <Line dataKey="p50" stroke="#1d4ed8" dot={false} strokeWidth={2} isAnimationActive={false} name="p50" />
            <ReferenceLine y={cushion} stroke="#dc2626" strokeDasharray="4 3" label={{ value: `cushion $${Math.round(cushion).toLocaleString()}`, fontSize: 11, fill: "#dc2626", position: "insideTopLeft" }} />
            <ReferenceLine y={0} stroke="#6b7280" />
            {f.events.map((e, i) => {
              const x = xOf(e.day);
              const pt = pos.get(x);
              if (!pt) return null;
              const slot = `${e.kind}|${x}`;
              if (seen.has(slot)) return null;
              seen.add(slot);
              return <ReferenceDot key={i} x={x} y={pt.p50} r={5} fill={EVENT_COLOR[e.kind] ?? DEFAULT_COLOR} stroke="#fff" ifOverflow="visible" />;
            })}
          </ComposedChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
