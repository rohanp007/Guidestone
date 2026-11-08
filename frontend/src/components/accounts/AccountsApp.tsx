"use client";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { forecastLiquid, getMe, getProfile, logout, type LiquidForecast, type Me, type Profile } from "@/lib/api";
import AccountList from "./AccountList";
import CashChart from "./CashChart";
import NetWorthChart from "./NetWorthChart";
import Statement from "./Statement";
import TopBar, { type Tab } from "./TopBar";
import Transactions, { type TxFilters } from "./Transactions";

type Loaded = { id: string; profile: Profile; forecast: LiquidForecast | null };

export default function AccountsApp() {
  const router = useRouter();
  const pathname = usePathname();
  const sp = useSearchParams();
  const [me, setMe] = useState<Me | null>(null);
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  const [error, setError] = useState<string | null>(null);

  const persona = me?.customer_id ?? "";                       // a customer only ever sees their own data
  const tab = (sp.get("tab") as Tab) || "accounts";
  const filters: TxFilters = useMemo(
    () => ({ account: sp.get("acct") ?? "", flow: sp.get("flow") ?? "", category: sp.get("cat") ?? "", q: sp.get("q") ?? "", page: Math.max(1, Number(sp.get("page")) || 1) }),
    [sp],
  );

  // router.replace is async: two quick changes must build on each other, not on the stale address bar
  const pending = useRef<URLSearchParams | null>(null);
  useEffect(() => { pending.current = null; }, [sp]);

  const setUrl = useCallback((patch: Record<string, string | number | null>) => {
    const q = pending.current ?? new URLSearchParams(window.location.search);
    pending.current = q;
    for (const [k, v] of Object.entries(patch)) {
      if (v === null || v === "" || (k === "page" && Number(v) <= 1)) q.delete(k); else q.set(k, String(v));
    }
    const s = q.toString();
    router.replace(s ? `${pathname}?${s}` : pathname, { scroll: false });
  }, [router, pathname]);

  const onFilters = useCallback((patch: Partial<TxFilters>) => {
    setUrl({
      ...(patch.account !== undefined && { acct: patch.account }), ...(patch.flow !== undefined && { flow: patch.flow }),
      ...(patch.category !== undefined && { cat: patch.category }), ...(patch.q !== undefined && { q: patch.q }), ...(patch.page !== undefined && { page: patch.page }),
    });
  }, [setUrl]);

  useEffect(() => { getMe().then(setMe).catch((e) => setError(String(e))); }, []);   // a 401 sends the browser to /login

  useEffect(() => {
    if (!persona) return;
    let live = true;
    Promise.all([getProfile(persona), forecastLiquid(persona, { with_backtest: false }).then((r) => r.result).catch(() => null)])
      .then(([profile, forecast]) => { if (live) { setLoaded({ id: persona, profile, forecast }); setError(null); } })
      .catch((e) => live && setError(String(e)));
    return () => { live = false; };
  }, [persona]);

  const signOut = async () => {
    try { await logout(); } finally { router.replace("/login"); }
  };
  const stale = loaded && loaded.id !== persona;

  return (
    <>
      <TopBar name={me ? `${me.first_name} ${me.last_name}` : ""} onSignOut={signOut} tab={tab} onTab={(t) => setUrl({ tab: t === "accounts" ? null : t })} />
      <main className="mx-auto w-full max-w-[1120px] flex-1 px-4 py-10 sm:px-6 sm:py-12">
        {error && <p role="alert" className="mb-6 text-[14px] text-[#b4423a]">Could not load: {error}</p>}
        {!loaded && !error && <p className="label">Loading…</p>}
        {loaded && (
          <div key={loaded.id} className="fade-in space-y-12 transition-opacity duration-200 sm:space-y-16"
            style={{ opacity: stale ? 0.5 : 1, transitionTimingFunction: "var(--ease)" }}>
            <Statement profile={loaded.profile} />
            <AccountList profile={loaded.profile} />
            <div className="space-y-8">
              {loaded.forecast && <CashChart forecast={loaded.forecast} />}
              <NetWorthChart profile={loaded.profile} />
            </div>
            <Transactions profile={loaded.profile} filters={filters} onFilters={onFilters} />
          </div>
        )}
      </main>
    </>
  );
}
