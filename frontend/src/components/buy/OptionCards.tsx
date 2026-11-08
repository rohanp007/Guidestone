"use client";
import { dateLong, money0 } from "@/lib/format";
import { FIGURES, FIGURE_LABEL, type BuyView, type Figure, type Pick } from "@/lib/buy";

const NA = "Not available";
function show(f: string, v: Figure["value"]): string {
  if (v === null || v === undefined) return NA;
  if (f === "msrp") return money0(Number(v));
  if (f === "mpg_combined") return `${v} mpg`;
  if (f === "nhtsa_stars") return `${v} of 5 stars`;
  if (f === "model_year") return String(v);
  return String(v);
}

/** Every figure with its value (or "Not available") and where it was found. */
function Figures({ pick }: { pick: Pick }) {
  return (
    <details className="mt-3 text-[14px]">
      <summary className="cursor-pointer select-none font-medium text-navy">Sources</summary>
      <ul className="mt-2 space-y-2">
        {FIGURES.map((f) => {
          const g = pick.figures[f];
          return (
            <li key={f} className="grid grid-cols-[8.5rem_1fr] gap-x-3">
              <span className="text-ink-2">{FIGURE_LABEL[f]}</span>
              <span>
                {g?.source ? <a href={g.source.url} target="_blank" rel="noopener noreferrer" className="text-navy underline underline-offset-2">{g.source.title}</a> : <span className="text-ink-2">No source</span>}
                {g?.source && <span className="block text-[13px] text-ink-2">Fetched {dateLong(g.source.fetchedAt)}</span>}
              </span>
            </li>
          );
        })}
      </ul>
    </details>
  );
}

function Stats({ pick }: { pick: Pick }) {
  return (
    <dl className="mt-3 grid grid-cols-[auto_1fr] items-baseline gap-x-4 gap-y-1.5 text-[14px]">
      {FIGURES.map((f) => {
        const v = show(f, pick.figures[f]?.value ?? null);
        return (
          <div key={f} className="contents">
            <dt className="text-ink-2">{FIGURE_LABEL[f]}</dt>
            <dd className={`num text-right ${v === NA ? "text-ink-2" : "font-medium"}`}>{v}</dd>
          </div>
        );
      })}
    </dl>
  );
}

/** Up to three researched cars. Selecting one loads its plans. Each card shows the cited figures; a missing one reads
 *  "Not available", never 0 or blank. */
export default function OptionCards({ view, selectedId, onSelect, busy }: { view: BuyView; selectedId: string; onSelect: (id: string) => void; busy: boolean }) {
  return (
    <ul className="grid gap-4 sm:grid-cols-2">
      {view.picks.slice(0, 3).map((p) => {
        const on = p.id === selectedId;
        return (
          <li key={p.id} className={`rounded-[6px] border bg-surface p-4 ${on ? "border-navy" : "border-line-strong"}`}>
            <button type="button" disabled={busy} aria-pressed={on} onClick={() => onSelect(p.id)} className="press block w-full text-left">
              <span className="flex items-center justify-between gap-2">
                <span className="text-[18px] font-medium">{p.name}</span>
                {on && <span className="rounded-[6px] border border-navy px-1.5 py-px text-[12px] font-medium text-navy">Selected</span>}
              </span>
              <span className="mt-2 block text-[15px] text-ink-2">{p.why}</span>
            </button>
            <Stats pick={p} />
            <p className="mt-2 text-[12px] text-ink-2">Starting price excludes destination, tax and fees.</p>
            <Figures pick={p} />
          </li>
        );
      })}
    </ul>
  );
}
