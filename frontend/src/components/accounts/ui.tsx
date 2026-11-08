"use client";
import { useEffect, useState, type ReactNode } from "react";

export type Load<T> = { data: T | null; error: boolean };

/** Runs `fn` once on mount. `error` is true when it rejected (a 401 has already sent the browser to /login). */
export function useLoad<T>(fn: () => Promise<T>): Load<T> {
  const [state, set] = useState<Load<T>>({ data: null, error: false });
  useEffect(() => {
    let live = true;
    fn().then((data) => live && set({ data, error: false })).catch(() => live && set({ data: null, error: true }));
    return () => { live = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- one load per mount
  }, []);
  return state;
}

/** A page section: a small caps heading over a hairline, then the content. */
export function Section({ title, aside, id, children }: { title: string; aside?: ReactNode; id: string; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="border-t border-line-strong pt-3">
      <div className="flex items-baseline justify-between gap-4">
        <h2 id={id} className="label">{title}</h2>
        {aside && <div className="text-[13px] text-ink-2">{aside}</div>}
      </div>
      <div className="mt-4">{children}</div>
    </section>
  );
}

export const Skeleton = ({ className = "" }: { className?: string }) => <div aria-hidden className={`skeleton ${className}`} />;

export const Unavailable = ({ children }: { children: ReactNode }) => <p role="status" className="text-[15px] text-ink-2">{children}</p>;

export type Tone = "good" | "fair" | "poor";
const TONE_COLOR: Record<Tone, string> = { good: "var(--good)", fair: "var(--fair)", poor: "var(--poor)" };

/** Status colour is never alone: every tone has its own shape (check, clock, alert) and the text label sits beside it. */
export function ToneIcon({ tone }: { tone: Tone }) {
  const common = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: TONE_COLOR[tone], strokeWidth: 1.75, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;
  if (tone === "good") return <svg {...common}><circle cx="8" cy="8" r="6.25" /><path d="M5.2 8.2l2 2 3.6-4" /></svg>;
  if (tone === "fair") return <svg {...common}><circle cx="8" cy="8" r="6.25" /><path d="M8 4.6V8l2.3 1.5" /></svg>;
  return <svg {...common}><path d="M8 1.8l6.4 11.4H1.6z" /><path d="M8 6.4v3.2M8 11.4v.1" /></svg>;
}

export const Badge = ({ tone, children }: { tone: Tone; children: ReactNode }) => (
  <span className="inline-flex items-center gap-1.5 rounded-[6px] border border-line-strong bg-surface px-2 py-0.5 text-[14px] font-medium text-ink">
    <ToneIcon tone={tone} />{children}
  </span>
);
