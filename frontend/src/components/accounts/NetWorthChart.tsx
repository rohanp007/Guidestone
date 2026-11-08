"use client";
import { useMemo } from "react";
import { Area, CartesianGrid, ComposedChart, Line, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import type { Profile } from "@/lib/api";
import { compactMoney, money0, monthFull, monthLabel } from "@/lib/format";
import { C } from "@/lib/tokens";
import { AXIS, ChartCard, DRAW_MS, niceTicks, usePrefersReducedMotion, Swatch, TooltipCard } from "./chartkit";

export default function NetWorthChart({ profile }: { profile: Profile }) {
  const reduce = usePrefersReducedMotion();
  const data = useMemo(
    () => (profile.history ?? []).map((h) => ({
      x: h.month, label: monthLabel(h.month), net: h.net_worth, estimated: h.estimated_assets ?? 0, ledger: h.net_worth - (h.estimated_assets ?? 0),
    })),
    [profile],
  );
  const hasEstimated = data.some((d) => d.estimated > 0);
  const ticks = useMemo(() => niceTicks(Math.min(...data.map((d) => d.net)), Math.max(...data.map((d) => d.net))), [data]);
  return (
    <ChartCard
      title="Net worth"
      note="Last 24 months, from statements. Home and vehicle values are estimates."
      legend={<span className="inline-flex items-center gap-2"><Swatch kind="area" /> Net worth</span>}
      table={
        <table className="w-full text-left mono">
          <thead><tr><th className="py-1 pr-4">Month</th><th className="pr-4">Net worth</th><th className="pr-4">Ledger</th><th>Estimated</th></tr></thead>
          <tbody>{data.map((d) => <tr key={d.x} className="border-t border-line"><td className="py-1 pr-4">{d.label}</td><td className="pr-4">{money0(d.net)}</td><td className="pr-4">{money0(d.ledger)}</td><td>{money0(d.estimated)}</td></tr>)}</tbody>
        </table>
      }
    >
      <ResponsiveContainer>
        <ComposedChart data={data} margin={{ top: 8, right: 24, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={C.line} />
          <XAxis dataKey="label" padding={{ left: 16, right: 0 }} tickMargin={6} tick={AXIS} tickLine={false} axisLine={{ stroke: C.lineStrong }} minTickGap={28} />
          <YAxis tick={AXIS} tickLine={false} axisLine={false} width={52} tickFormatter={compactMoney} ticks={ticks} domain={[ticks[0], ticks[ticks.length - 1]]} interval={0} />
          <Tooltip
            cursor={{ stroke: C.ink3, strokeWidth: 1 }}
            isAnimationActive={false}
            content={({ active, payload }) => {
              const d = active ? payload?.[0]?.payload : undefined;
              if (!d) return null;
              return (
                <TooltipCard>
                  <div className="label !text-[11px]">{monthFull(d.x)}</div>
                  <div className="mt-1 text-[16px] font-medium">{money0(d.net)}</div>
                  <div className="mono text-ink-2">Ledger {money0(d.ledger)}</div>
                  {hasEstimated && <div className="mono text-ink-2">Estimated {money0(d.estimated)}</div>}
                </TooltipCard>
              );
            }}
          />
          <Area dataKey="net" stroke="none" fill={C.navyTint} fillOpacity={1} activeDot={false} isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
          <Line dataKey="net" stroke={C.navy} strokeWidth={2} dot={false} activeDot={{ r: 4, fill: C.navy, stroke: C.surface, strokeWidth: 2 }}
            isAnimationActive={!reduce} animationDuration={DRAW_MS} animationEasing="ease-out" />
        </ComposedChart>
      </ResponsiveContainer>
    </ChartCard>
  );
}
