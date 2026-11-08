"use client";
import type { ReactNode } from "react";

export function Section({ title, children, open = false }: { title: string; children: ReactNode; open?: boolean }) {
  return (
    <details open={open} className="border border-gray-300 rounded mb-3">
      <summary className="cursor-pointer select-none px-3 py-2 font-semibold bg-gray-100 text-gray-900">{title}</summary>
      <div className="p-3 overflow-x-auto">{children}</div>
    </details>
  );
}

export const money = (n: number | null | undefined) =>
  n == null ? "–" : n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
export const pct = (n: number | null | undefined, d = 1) => (n == null ? "–" : `${(n * 100).toFixed(d)}%`);

export function KV({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <table className="text-sm">
      <tbody>
        {rows.map(([k, v]) => (
          <tr key={k}>
            <td className="pr-6 py-0.5 text-gray-600">{k}</td>
            <td className="py-0.5 font-mono">{v}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <table className="text-sm border-collapse">
      <thead>
        <tr>{head.map((h) => <th key={h} className="text-left border-b border-gray-300 pr-4 py-1">{h}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((r, i) => (
          <tr key={i} className="odd:bg-gray-50 dark:odd:bg-transparent">
            {r.map((c, j) => <td key={j} className="pr-4 py-0.5 font-mono whitespace-nowrap">{c}</td>)}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export const Json = ({ value }: { value: unknown }) => (
  <pre className="text-xs bg-gray-100 text-gray-900 p-2 rounded max-h-96 overflow-auto">{JSON.stringify(value, null, 2)}</pre>
);
