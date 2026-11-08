"use client";
import { runAgent, type Advice } from "@/lib/api";
import { Badge, Skeleton, Unavailable, useLoad, type Tone } from "./ui";

const VERDICT: Record<Advice["verdict"], { label: string; tone: Tone }> = {
  act_now: { label: "Act now", tone: "poor" },
  plan_ahead: { label: "Plan ahead", tone: "fair" },
  on_track: { label: "On track", tone: "good" },
};

/** The advisor can take 20+ seconds, so the page never waits for it. A newer run replacing ours (409, e.g. the sign-in warm-up) is
 *  expected: ask again and get that run's result. */
async function fetchAdvice(): Promise<Advice> {
  for (let attempt = 0; attempt < 3; attempt++) {
    const r = await runAgent("dashboard_open");
    if (r) {
      if (!r.result.advice) throw new Error("no advice");
      return r.result.advice;
    }
    await new Promise((res) => setTimeout(res, 1000));
  }
  throw new Error("superseded");
}

export default function AdvisorCard() {
  const { data, error } = useLoad(fetchAdvice);
  return (
    <section aria-labelledby="advisor-h" className="rounded-[6px] border border-line-strong bg-surface p-5 sm:p-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 id="advisor-h" className="label">What to do next</h2>
        {data && <Badge tone={VERDICT[data.verdict].tone}>{VERDICT[data.verdict].label}</Badge>}
      </div>
      <div className="mt-4" aria-live="polite" aria-busy={!data && !error}>
        {error && <Unavailable>Recommendations are unavailable right now.</Unavailable>}
        {!data && !error && (
          <div>
            <p className="text-[15px] text-ink-2">Preparing your recommendations…</p>
            <div className="mt-4 space-y-5">{[0, 1, 2].map((i) => <div key={i} className="space-y-2"><Skeleton className="h-5 w-2/3" /><Skeleton className="h-4 w-full" /></div>)}</div>
          </div>
        )}
        {data && (
          <ol className="fade-in space-y-5">
            {data.moves.slice(0, 3).map((m, i) => (
              <li key={m.move_code} className="grid grid-cols-[1.5rem_1fr] gap-x-2">
                <span className="num pt-0.5 text-[15px] text-ink-2">{i + 1}.</span>
                <div>
                  <h3 className="text-[17px] font-medium leading-snug">{m.title_text}</h3>
                  <p className="mt-1 text-[15px] text-ink-2">{m.why_text}</p>
                </div>
              </li>
            ))}
            {data.moves.length === 0 && <li className="text-[15px] text-ink-2">Nothing needs your attention right now.</li>}
          </ol>
        )}
      </div>
    </section>
  );
}
