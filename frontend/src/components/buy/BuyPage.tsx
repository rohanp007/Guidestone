"use client";
import Link from "next/link";
import { useMemo } from "react";
import AgentActivity from "@/components/agent/AgentActivity";
import { Unavailable } from "@/components/accounts/ui";
import PageShell from "@/components/shell/PageShell";
import type { AgentActivityHandle } from "@/lib/useAgentActivity";
import OptionCards from "./OptionCards";
import PlanSection from "./PlanSection";
import SearchForm from "./SearchForm";
import { useBuy } from "./useBuy";

const ORDER = ["orchestrator", "researcher", "compute", "advisor", "presenter"];

function Body() {
  const b = useBuy();
  const { view } = b;
  // the window lists agents in the order they work on a purchase: Orchestrator, Researcher, Compute, Advisor
  const agent: AgentActivityHandle = useMemo(() => ({ ...b.agent, activity: { ...b.agent.activity, rows: [...b.agent.activity.rows].sort((x, y) => ORDER.indexOf(x.agent) - ORDER.indexOf(y.agent)) } }), [b.agent]);
  const noOptions = !!view && view.picks.length === 0;

  return (
    <div className="fade-in py-8 sm:py-10">
      <Link href="/simulate" className="text-[14px] text-ink-2 hover:text-navy">← Simulate</Link>
      <p className="label mt-4">Buy a car</p>
      <p className="mt-2 max-w-[56ch] text-[20px] font-medium leading-snug text-navy">Find cars that fit, then see how to pay for one.</p>
      <div className="lg:hidden"><AgentActivity h={agent} variant="sheet" /></div>
      <div className="h-12 lg:hidden" aria-hidden />
      <div className="mt-8 grid gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,1fr)_340px]">
        <aside className="hidden lg:order-last lg:block"><div className="lg:sticky lg:top-4"><AgentActivity h={agent} variant="panel" /></div></aside>
        <div className="min-w-0 space-y-12">
          <SearchForm onSubmit={b.run} busy={b.loading} query={view?.query ?? null} />
          {b.failed && <Unavailable>We couldn’t complete that search just now. {view ? "Showing your last results. " : ""}Try again, or raise the budget.</Unavailable>}
          {noOptions && <p role="status" className="text-[16px]">Nothing in this segment fits that budget with a cited price. Try raising the budget.</p>}
          {view && !noOptions && (
            <div className="space-y-12 transition-opacity duration-200" style={{ opacity: b.loading ? 0.5 : 1, transitionTimingFunction: "var(--ease)" }} aria-busy={b.loading}>
              <section aria-labelledby="options-h">
                <h2 id="options-h" className="label">Options</h2>
                <div className="mt-4"><OptionCards view={view} selectedId={view.choiceId} busy={b.loading}
                  onSelect={(id) => b.lastParams && b.run({ ...b.lastParams, choice_id: id })} /></div>
              </section>
              <PlanSection view={view} />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

export default function BuyPage() {
  return <PageShell active="simulate" title="Buy a car">{() => <Body />}</PageShell>;
}
