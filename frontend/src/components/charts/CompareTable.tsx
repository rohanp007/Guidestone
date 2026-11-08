"use client";
import type { ReactNode } from "react";

export type CompareCell = { key: string; label: string; value: ReactNode; align?: "right" };
export type CompareRowData = { key: string; title: string; subtitle?: string; recommended?: boolean; cells: CompareCell[]; notes?: ReactNode };

type Props = {
  rows: CompareRowData[];
  highlightedKey: string | null;        // the recommended row (left border + label)
  selectedKey: string | null;           // the row the page is showing
  onSelect?: (key: string) => void;
  caption: string;
};

/** Rows of options with the same columns. A table from the `sm` breakpoint up, stacked cards on a phone. Clicking (or pressing Enter on)
 *  a row selects it. Data-driven: no fetching. */
export default function CompareTable({ rows, highlightedKey, selectedKey, onSelect, caption }: Props) {
  const cols = rows[0]?.cells ?? [];
  const select = (k: string) => onSelect?.(k);
  const Title = ({ r }: { r: CompareRowData }) => (
    <button type="button" onClick={() => select(r.key)} aria-pressed={r.key === selectedKey} className="press text-left">
      <span className="block text-[15px] font-medium">{r.title}{r.recommended && <span className="ml-2 rounded-[6px] border border-navy px-1.5 py-px text-[12px] font-medium text-navy">Recommended</span>}</span>
      {r.subtitle && <span className="block text-[13px] text-ink-2">{r.subtitle}</span>}
    </button>
  );
  const rowClass = (r: CompareRowData) => `cursor-pointer border-l-[3px] ${r.key === highlightedKey ? "border-l-navy" : "border-l-transparent"} ${r.key === selectedKey ? "bg-navy-tint" : "row-hover"}`;

  return (
    <div>
      <table className="hidden w-full table-fixed text-left text-[14px] sm:table">
        <caption className="sr-only">{caption}</caption>
        <thead><tr className="label border-b border-line-strong">
          <th scope="col" className="w-[24%] py-2 pl-3 pr-3 font-normal">Strategy</th>
          {cols.map((c) => <th key={c.key} scope="col" className={`pr-3 font-normal ${c.align === "right" ? "text-right" : ""}`}>{c.label}</th>)}
          <th scope="col" className="w-[26%] font-normal">Notes</th>
        </tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.key} onClick={() => select(r.key)} className={`border-b border-line-soft ${rowClass(r)}`} aria-selected={r.key === selectedKey}>
              <th scope="row" className="py-3 pl-3 pr-3 font-normal"><Title r={r} /></th>
              {r.cells.map((c) => <td key={c.key} className={`num pr-3 ${c.align === "right" ? "text-right" : ""}`}>{c.value}</td>)}
              <td className="py-2">{r.notes}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <ul className="space-y-3 sm:hidden" aria-label={caption}>
        {rows.map((r) => (
          <li key={r.key} onClick={() => select(r.key)} className={`rounded-[6px] border border-line-strong px-3 py-3 ${rowClass(r)}`}>
            <Title r={r} />
            <dl className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5">
              {r.cells.map((c) => <div key={c.key}><dt className="text-[12px] text-ink-2">{c.label}</dt><dd className="num text-[15px]">{c.value}</dd></div>)}
            </dl>
            {r.notes && <div className="mt-2">{r.notes}</div>}
          </li>
        ))}
      </ul>
    </div>
  );
}
