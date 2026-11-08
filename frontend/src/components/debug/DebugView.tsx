"use client";
import { useEffect, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  forecastLiquid, forecastLiquidSummary, getAssumptions, getProfile, listProfiles, paycheckToPaycheck,
  type AssumptionsResponse, type LiquidForecast, type PaycheckToPaycheck, type Profile, type ProfileListItem,
} from "@/lib/api";
import ForecastChart from "./ForecastChart";
import FormulaRunner from "./FormulaRunner";
import { Json, KV, Section, Table, money, pct } from "./ui";

const ESTIMATED = new Set(["home", "vehicle"]);

export default function DebugView({ id }: { id: string }) {
  const router = useRouter();
  const [list, setList] = useState<ProfileListItem[]>([]);
  const [profile, setProfile] = useState<Profile | null>(null);
  const [p2p, setP2p] = useState<PaycheckToPaycheck | null>(null);
  const [fc, setFc] = useState<{ result: LiquidForecast; computeMs: number; cached: boolean } | null>(null);
  const [summary, setSummary] = useState<unknown>(null);
  const [assump, setAssump] = useState<AssumptionsResponse | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    listProfiles().then(setList).catch((e) => setErr(String(e)));
    getAssumptions().then(setAssump).catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    let live = true;
    getProfile(id).then((p) => live && setProfile(p)).catch((e) => live && setErr(String(e)));
    paycheckToPaycheck(id).then((r) => live && setP2p(r.result)).catch((e) => live && setErr(String(e)));
    forecastLiquid(id, { with_backtest: true }).then((r) => live && setFc(r)).catch((e) => live && setErr(String(e)));
    forecastLiquidSummary(id, { with_backtest: true }).then((r) => live && setSummary(r.result)).catch((e) => live && setErr(String(e)));
    return () => { live = false; };
  }, [id]);

  const s = profile?.summary;
  const sp = profile?.spending;
  const held = (assump?.calibration_details as { held_out?: Record<string, number>; nominal?: number; backtest_error?: Record<string, { median: number; p75: number; p90: number; customers: number }> } | null | undefined);

  return (
    <main className="max-w-6xl mx-auto p-4 text-gray-900 dark:text-gray-100">
      <header className="flex items-center gap-3 mb-4">
        <h1 className="text-xl font-bold">Debug</h1>
        <select className="border px-2 py-1 bg-white text-gray-900" value={id} onChange={(e) => router.push(`/debug/${e.target.value}`)}>
          {list.map((p) => (
            <option key={p.id} value={p.id}>
              {p.id} · {p.age} · {p.employment} · {p.housing}{p.flags.length ? ` · ${p.flags.join(",")}` : ""}
            </option>
          ))}
        </select>
        <Link href="/" className="text-sm underline">home</Link>
      </header>
      {err && <p className="text-red-600 mb-3">Error: {err}</p>}
      {!profile && !err && <p>Loading…</p>}

      {profile && (
        <>
          <Section title="Summary" open>
            <KV rows={[
              ["age / employment / housing", `${profile.age} / ${profile.employment} / ${profile.housing}`],
              ["credit", `${profile.credit_score} (${profile.credit_tier})`],
              ["as of", profile.as_of_date],
              ["liquid cash", money(s?.liquid_cash)],
              ["total assets", money(s?.total_assets)],
              ["total debt", money(s?.total_debt)],
              ["net worth", money(s?.net_worth)],
              ["net monthly income", money(s?.net_monthly_income)],
              ["monthly non-debt expenses", money(s?.monthly_non_debt_expenses)],
              ["monthly debt payments", money(s?.monthly_debt_payments)],
              ["monthly surplus", money(s?.monthly_surplus)],
              ["credit utilization", pct(s?.credit_utilization)],
            ]} />
          </Section>

          <Section title={`Accounts (${profile.accounts.length})`}>
            <Table
              head={["id", "type", "subtype", "balance", "rate", "min pmt", "limit", "remaining", "maturity"]}
              rows={profile.accounts.map((a) => [
                a.id,
                ESTIMATED.has(a.type) ? <span key="t">{a.type} <em className="text-amber-700 not-italic">(estimated asset)</em></span> : a.type,
                a.subtype ?? "", money(a.balance), a.rate ? pct(a.rate, 2) : "", a.min_payment ? money(a.min_payment) : "",
                a.credit_limit ? money(a.credit_limit) : "", a.term_months_remaining ?? "", a.maturity_date ?? "",
              ])}
            />
          </Section>

          <Section title={`Recently ended obligations (${profile.ended_recurring?.length ?? 0})`} open={!!profile.ended_recurring?.length}>
            {profile.ended_recurring?.length ? (
              <>
                <Table
                  head={["description", "kind", "monthly_amount", "last_date"]}
                  rows={profile.ended_recurring.map((r) => [r.description, r.kind, money(r.monthly_amount), r.last_date])}
                />
                <p className="text-xs text-gray-500 mt-1">Stopped before {profile.as_of_date}. Not in the forecast, so the freed cash shows up as a rising balance.</p>
              </>
            ) : <p className="text-gray-500">none</p>}
          </Section>

          <Section title={`Recurring (${profile.recurring?.length ?? 0})`}>
            <p className="text-xs text-gray-500 mb-2">
              Insurance descriptions (insurer names) are a <b>generator artifact</b>: the synthetic data picks the insurer at random each month, so don&apos;t trust them.
            </p>
            <Table
              head={["description", "kind", "cadence", "monthly_amount", "months_seen", "last_date"]}
              rows={(profile.recurring ?? []).map((r) => [
                r.kind === "insurance" || r.category === "insurance"
                  ? <span key="d">{r.description} <em className="text-xs text-gray-500 not-italic">(generator artifact)</em></span>
                  : r.description,
                r.kind, r.cadence, money(r.monthly_amount), r.months_seen, r.last_date,
              ])}
            />
          </Section>

          <Section title="Spending profile">
            {sp ? (
              <div className="space-y-4 text-sm">
                <KV rows={[
                  ["buffer ratio (min / median)", `${sp.buffer_ratio_min.toFixed(3)} / ${sp.buffer_ratio_median.toFixed(3)}`],
                  ["card share", pct(sp.card_share)],
                  ["carries card balance", `${sp.carries_card_balance} (${sp.card_carry_months} months)`],
                  ["savings sweeps / pulls per year", `${sp.savings_sweeps_per_year} / ${sp.savings_pulls_per_year}`],
                  ["overdraft top-ups", sp.overdraft_topups ?? 0],
                  ["subscription total", money(sp.subscription_total)],
                ]} />
                <div><b>Subscriptions</b><Table head={["name", "monthly"]} rows={(sp.subscriptions ?? []).map((x) => [x.name, money(x.monthly_amount)])} /></div>
                <div><b>Notable trends</b>{sp.notable_trends?.length
                  ? <Table head={["category", "annual", "monthly"]} rows={sp.notable_trends.map((t) => [t.category, pct(t.annual_change), money(t.monthly_change)])} />
                  : <p className="text-gray-500">none</p>}</div>
                <div><b>Top categories</b><Table head={["category", "monthly", "share"]} rows={(sp.top_categories ?? []).map((c) => [c.category, money(c.monthly), pct(c.share)])} /></div>
                <div><b>Volatility (CV)</b><Json value={sp.volatility} /></div>
                <div><b>Seasonality</b><Json value={sp.seasonality} /></div>
              </div>
            ) : <p>No spending profile.</p>}
          </Section>

          <Section title="Paycheck-to-paycheck">
            {p2p ? (
              <div className="text-sm">
                <p className="font-mono">flag: <b>{String(p2p.flag)}</b> · score: {p2p.score.toFixed(2)}</p>
                <ul className="list-disc ml-5">{p2p.reasons.map((r) => <li key={r}>{r}</li>)}</ul>
              </div>
            ) : <p>Loading…</p>}
          </Section>

          <Section title="Forecast" open>
            {fc ? (
              <div className="space-y-4 text-sm">
                <p className="font-mono">X-Compute-Ms: {fc.cached ? "cached" : `${fc.computeMs}ms`} · horizon {fc.result.horizon_months} mo · {fc.result.paths} paths · start {money(fc.result.start_liquid)}</p>
                <ForecastChart f={fc.result} />
                <p className="text-xs text-gray-500">Blue band = p10–p90, line = p50, dashed red = cushion (one month of essentials), orange dots = events.</p>
                <div>
                  <b>Risk</b>
                  <Table
                    head={["", "buffer", "P(ever)", "P(90d)", "p50 days below", "p50 last below", "first p50", "first p10"]}
                    rows={([["overdraft", fc.result.overdraft_risk], ["cushion", fc.result.cushion_risk]] as const).map(([n, r]) => [
                      n, money(r.buffer), pct(r.probability), pct(r.probability_90d), pct(r.share_of_days_below),
                      r.last_date_below_p50 ?? "never", r.first_date_p50 ?? "never", r.first_date_p10 ?? "never",
                    ])}
                  />
                  <p className="mt-2">P(below cushion) at month-end:</p>
                  <p className="font-mono text-xs">
                    {fc.result.cushion_risk.month_end_probability_by_month?.map((m) => `${m.month} ${pct(m.probability, 0)}`).join(" · ") ?? "not in this response (restart the API?)"}
                  </p>
                  <p className="text-xs text-gray-500">P(ever) counts any day below the line; month-end probabilities can be far lower when the dip is mid-month, before payday.</p>
                </div>
                <div>
                  <b>Average monthly change (uncalibrated)</b>
                  <p className="font-mono">{money(fc.result.monthly_net.mean)} (p10 {money(fc.result.monthly_net.p10)} – p90 {money(fc.result.monthly_net.p90)})</p>
                  <p className="text-xs text-gray-500">Each path&apos;s average over the whole horizon, so the spread is narrow. It is not the band on the chart.</p>
                </div>
                <div>
                  <b>Events</b>
                  <Table
                    head={["day", "kind", "description", "monthly impact"]}
                    rows={fc.result.events.map((e) => [
                      e.day,
                      e.kind,
                      e.kind === "raise" || e.kind === "rent_renewal"
                        ? <span key="d">{e.description} <em className="text-xs text-gray-500 not-italic">(assumed timing: as-of anniversary)</em></span>
                        : e.description,
                      e.monthly_impact != null ? money(e.monthly_impact) : "",
                    ])}
                  />
                </div>
                <div><b>Summary JSON</b><Json value={summary} /></div>

                <div className="border rounded p-3">
                  <b>Raw (uncalibrated) — per-customer backtest</b>
                  {fc.result.backtest ? <Json value={fc.result.backtest} /> : <p className="text-gray-500">no backtest for this customer</p>}
                  <p className="text-xs text-gray-500 mt-1">Do not quote these coverage numbers; only the all-customer calibrated numbers below are valid.</p>
                </div>
                <div className="border rounded p-3">
                  <b>Calibrated, all-customer (held-out) — from /api/assumptions</b>
                  {held?.held_out ? (
                    <KV rows={[
                      ["nominal band", pct(held.nominal, 0)],
                      ["month-end coverage, held-out (calibrated)", pct(held.held_out.held_out_month_end, 0)],
                      ["month-end coverage, raw", pct(held.held_out.uncalibrated_month_end, 0)],
                      ["all-days coverage, held-out (calibrated)", pct(held.held_out.held_out_all_days, 0)],
                      ["all-days coverage, raw", pct(held.held_out.uncalibrated_all_days, 0)],
                    ]} />
                  ) : <p>Loading…</p>}
                  {assump?.calibration && <p className="text-xs text-gray-500 mt-1">{assump.calibration}</p>}
                  {held?.backtest_error && (
                    <div className="mt-2">
                      <Table
                        head={["horizon", "median error", "p75", "p90", "customers"]}
                        rows={Object.entries(held.backtest_error).map(([h, v]) => [h, pct(v.median), pct(v.p75), pct(v.p90), v.customers])}
                      />
                      <p className="text-xs text-gray-500 mt-1">Error = stricter of mean daily and cumulative-net-change error, as a share of one month&apos;s outflow, per customer then summarized.</p>
                    </div>
                  )}
                </div>
              </div>
            ) : <p>Running forecast…</p>}
          </Section>
        </>
      )}

      <Section title="Formula runner" open>
        <FormulaRunner id={id} />
      </Section>
    </main>
  );
}
