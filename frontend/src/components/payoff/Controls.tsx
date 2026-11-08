"use client";
import { useId, useState } from "react";
import { money0 } from "@/lib/format";
import { STRATEGY_CODES, STRATEGY_NAME, type PayoffView, type StrategyCode } from "@/lib/payoff";
import { ToneIcon } from "@/components/accounts/ui";
import type { Controls as ControlState, Defaults } from "./usePayoff";

const STEP = 25;
const clamp = (n: number, max: number) => Math.min(max, Math.max(0, Math.round(n / STEP) * STEP));

function MoneyField({ id, label, value, max, onChange, disabled }: { id: string; label: string; value: number; max: number; onChange: (n: number) => void; disabled?: boolean }) {
  const [draft, setDraft] = useState<string | null>(null);
  const commit = () => {
    if (draft !== null) { const n = Number(draft.replace(/[^0-9.]/g, "")); if (Number.isFinite(n) && draft.trim() !== "") onChange(clamp(n, max)); }
    setDraft(null);
  };
  return (
    <span className="relative inline-block w-32">
      <span aria-hidden className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-ink-2">$</span>
      <input id={id} aria-label={label} inputMode="numeric" disabled={disabled}
        value={draft ?? String(Math.round(value))} onFocus={(e) => e.currentTarget.select()}
        onChange={(e) => setDraft(e.target.value)} onBlur={commit} onKeyDown={(e) => { if (e.key === "Enter") commit(); }}
        className="num w-full rounded-[6px] border border-line-strong bg-surface py-1.5 pl-6 pr-2 text-right text-[16px] outline-none focus-visible:border-navy disabled:opacity-50" />
    </span>
  );
}

function MoneySlider({ label, value, max, mark, markLabel, onChange, disabled, note }: {
  label: string; value: number; max: number; mark?: number; markLabel?: string; onChange: (n: number) => void; disabled?: boolean; note?: React.ReactNode;
}) {
  const id = useId();
  const pct = max > 0 ? Math.min(100, (value / max) * 100) : 0;
  const markPct = mark !== undefined && max > 0 ? Math.min(100, (mark / max) * 100) : null;
  return (
    <div>
      <div className="flex items-center justify-between gap-3">
        <label htmlFor={id} className="text-[15px] font-medium">{label}</label>
        <MoneyField id={`${id}-n`} label={`${label}, dollars`} value={value} max={max} onChange={onChange} disabled={disabled} />
      </div>
      <div className="relative mt-1">
        <input id={id} type="range" className="range" min={0} max={max} step={STEP} value={Math.min(value, max)} disabled={disabled}
          style={{ ["--fill" as string]: `${pct}%` }} aria-valuetext={money0(value)} onChange={(e) => onChange(Number(e.target.value))} />
        {markPct !== null && (
          <span aria-hidden className="pointer-events-none absolute top-[17px] -translate-x-1/2" style={{ left: `calc(10px + (100% - 20px) * ${markPct / 100})` }}>
            <span className="block h-2 w-px bg-ink-2" />
          </span>
        )}
      </div>
      <div className="relative mt-1 flex justify-between text-[12px] text-ink-2"><span className="num">$0</span>{markLabel && mark !== undefined && <span>{markLabel} <span className="num">{money0(mark)}</span></span>}<span className="num">{money0(max)}</span></div>
      {note}
    </div>
  );
}

type Props = {
  controls: ControlState; view: PayoffView; defaults: Defaults | null; totalCash: number;
  mode: "full" | "extra_only";
  onChange: (patch: Partial<ControlState>) => void; onReset: () => void; loading: boolean; failed: boolean;
};

export default function Controls({ controls, view, defaults, totalCash, mode, onChange, onReset, loading, failed }: Props) {
  const surplus = defaults?.extra ?? view.extraMonthly;
  const safe = defaults?.lump ?? view.safeLumpSum;
  const extraMax = Math.max(STEP * Math.ceil((2 * surplus) / STEP), 500);
  const lumpMax = Math.max(STEP * Math.ceil(Math.max(totalCash, safe) / STEP), STEP);
  const extra = controls.extra ?? view.extraMonthly;
  const lump = controls.lump ?? view.lumpSum;
  const chosen = controls.strategy ?? view.chosen;
  const exceeds = view.warnings.includes("exceeds_safe_lump");
  const lumpOff = mode === "extra_only";

  return (
    <section aria-labelledby="controls-h" className="rounded-[6px] border border-line-strong bg-surface p-5">
      <div className="flex items-baseline justify-between">
        <h2 id="controls-h" className="label">Your plan</h2>
        <button type="button" onClick={onReset} className="text-[14px] font-medium text-navy underline-offset-2 hover:underline">Reset to recommended</button>
      </div>
      <div className="mt-5 space-y-7">
        <MoneySlider label="Extra per month" value={extra} max={extraMax} mark={surplus} markLabel="Your monthly surplus" onChange={(n) => onChange({ extra: n })} />
        <MoneySlider label="Lump sum" value={lump} max={lumpMax} mark={safe} markLabel="Safe amount" disabled={lumpOff} onChange={(n) => onChange({ lump: n })}
          note={exceeds && (
            <p role="status" className="mt-2 flex items-start gap-2 text-[14px] text-ink"><span className="mt-0.5 shrink-0"><ToneIcon tone="fair" /></span>
              This is more than the amount we think is safe to use ({money0(safe)}). It leaves less cash for bills and surprises.</p>
          )} />
        <fieldset disabled={lumpOff} className="min-w-0">
          <legend className="text-[15px] font-medium">Strategy</legend>
          <div className="mt-2 space-y-1.5">
            {STRATEGY_CODES.map((code: StrategyCode) => (
              <label key={code} className={`press flex cursor-pointer items-center gap-3 rounded-[6px] border px-3 py-2 text-[15px] has-[:checked]:border-navy has-[:checked]:bg-navy-tint has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-orange ${lumpOff ? "opacity-50" : "border-line-strong"}`}>
                <input type="radio" name="strategy" value={code} checked={chosen === code} onChange={() => onChange({ strategy: code })} className="sr-only" />
                <span aria-hidden className="num flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-line-strong text-[13px]">{code}</span>
                <span className="flex-1">{STRATEGY_NAME[code]}</span>
                {view.recommended === code && <span className="rounded-[6px] border border-navy px-1.5 py-px text-[12px] font-medium text-navy">Recommended</span>}
              </label>
            ))}
          </div>
        </fieldset>
      </div>
      <p className="mt-4 min-h-[1.25rem] text-[13px] text-ink-2" aria-live="polite">
        {failed ? "Couldn’t update just now. Showing your last results." : loading ? "Updating…" : ""}
      </p>
    </section>
  );
}
