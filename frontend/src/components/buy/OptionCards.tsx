"use client";
import { dateLong, money0 } from "@/lib/format";
import { FIGURES, FIGURE_LABEL, type BuyView, type Pick } from "@/lib/buy";

function Sources({ pick }: { pick: Pick }) {
  return (
    <details className="mt-3 text-[14px]">
      <summary className="cursor-pointer select-none font-medium text-navy">Sources</summary>
      <ul className="mt-2 space-y-2">
        {FIGURES.map((f) => {
          const s = pick.sources.find((x) => x.figure === f);
          return (
            <li key={f} className="grid grid-cols-[9.5rem_1fr] gap-x-3">
              <span className="text-ink-2">{FIGURE_LABEL[f]}</span>
              {s ? (
                <span>
                  <a href={s.url} target="_blank" rel="noopener noreferrer" className="text-navy underline underline-offset-2">{s.title}</a>
                  <span className="block text-[13px] text-ink-2">Fetched {dateLong(s.fetchedAt)}</span>
                </span>
              ) : <span className="text-ink-2">Not available</span>}
            </li>
          );
        })}
      </ul>
    </details>
  );
}

/** Up to three researched cars. Selecting one loads its plans. The API gives the cited figures as prose and as source lines, not as
 *  separate numbers, so a card shows the description and lists where each figure comes from. */
export default function OptionCards({ view, selectedId, onSelect, busy }: { view: BuyView; selectedId: string; onSelect: (id: string) => void; busy: boolean }) {
  return (
    <ul className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
      {view.picks.slice(0, 3).map((p) => {
        const on = p.id === selectedId;
        return (
          <li key={p.id} className={`rounded-[6px] border bg-surface p-4 ${on ? "border-navy" : "border-line-strong"}`}>
            <button type="button" disabled={busy} aria-pressed={on} onClick={() => onSelect(p.id)} className="press block w-full text-left">
              <span className="flex items-center justify-between gap-2">
                <span className="text-[18px] font-medium">{p.name}</span>
                {on && <span className="rounded-[6px] border border-navy px-1.5 py-px text-[12px] font-medium text-navy">Selected</span>}
              </span>
              {on && <span className="mt-1 block text-[14px]"><span className="num font-medium">{money0(view.car.price)}</span> <span className="text-ink-2">Starting price, excludes destination, tax and fees</span></span>}
              <span className="mt-2 block text-[15px] text-ink-2">{p.why}</span>
            </button>
            <Sources pick={p} />
          </li>
        );
      })}
    </ul>
  );
}
