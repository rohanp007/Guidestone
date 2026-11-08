"use client";
export type Tab = "accounts" | "simulate" | "plan";
const TABS: { id: Tab; label: string; ready: boolean }[] = [
  { id: "accounts", label: "Accounts", ready: true },
  { id: "simulate", label: "Simulate", ready: false },
  { id: "plan", label: "Plan", ready: false },
];

function Tabs({ tab, onTab }: { tab: Tab; onTab: (t: Tab) => void }) {
  const index = Math.max(0, TABS.findIndex((t) => t.id === tab));
  return (
    <div role="tablist" aria-label="Sections" className="relative grid grid-cols-3 rounded-full border border-line bg-surface p-0.5 text-[14px]">
      <span
        aria-hidden
        className="absolute top-0.5 bottom-0.5 left-0.5 rounded-full bg-navy"
        style={{ width: "calc((100% - 4px) / 3)", transform: `translateX(${index * 100}%)`, transition: "transform 250ms var(--ease)" }}
      />
      {TABS.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={t.id === tab}
          aria-disabled={!t.ready}
          title={t.ready ? undefined : "Not built yet"}
          onClick={() => t.ready && onTab(t.id)}
          className={`press relative z-10 rounded-full px-4 py-1.5 font-medium ${t.id === tab ? "text-white" : t.ready ? "text-ink-2" : "text-ink-3 cursor-not-allowed"}`}
          style={{ transition: "color 250ms var(--ease)" }}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export default function TopBar({ name, onSignOut, tab, onTab }: {
  name: string; onSignOut: () => void; tab: Tab; onTab: (t: Tab) => void;
}) {
  return (
    <header className="border-b border-line">
      <div className="mx-auto grid max-w-[1120px] grid-cols-1 items-center gap-3 px-4 py-3 sm:grid-cols-[1fr_auto_1fr] sm:px-6">
        <div className="text-[18px] font-semibold tracking-tight text-navy">Guidestone</div>
        <div className="justify-self-start sm:justify-self-center"><Tabs tab={tab} onTab={onTab} /></div>
        <div className="flex items-center gap-4 text-[14px] sm:justify-self-end">
          <span className="text-ink-2">{name}</span>
          <button type="button" onClick={onSignOut} className="press rounded-[6px] border border-line-strong bg-surface px-3 py-1.5 font-medium text-ink hover:border-navy">
            Sign out
          </button>
        </div>
      </div>
    </header>
  );
}
