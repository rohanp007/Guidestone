"use client";
import { useRouter } from "next/navigation";
import { getMe, getMyForecast, getMyProfile, getMyRecurring, getMyStatus, getMyTransactions, logout, type Transaction } from "@/lib/api";
import { isoOf, parseDay } from "@/lib/format";
import AccountGroups from "./AccountGroups";
import AdvisorCard from "./AdvisorCard";
import CashForecast from "./CashForecast";
import Header from "./Header";
import Recurring from "./Recurring";
import Spending from "./Spending";
import SummaryRow from "./SummaryRow";
import Transactions from "./Transactions";
import { Skeleton, useLoad } from "./ui";

/** Every page of a transactions query (the API caps a page at 200). */
async function fetchAll(params: { start: string; flow_class?: string }): Promise<Transaction[]> {
  const out: Transaction[] = [];
  for (let page = 1; ; page++) {
    const r = await getMyTransactions({ ...params, page, page_size: 200 });
    out.push(...r.items);
    if (out.length >= r.total || r.items.length === 0) return out;
  }
}
let meOnce: ReturnType<typeof getMe> | null = null;
/** One /api/me request shared by everything on the page. */
const getMeOnce = () => (meOnce ??= getMe().catch((e) => { meOnce = null; throw e; }));
const daysBefore = (iso: string, n: number) => { const d = parseDay(iso); d.setDate(d.getDate() - n); return isoOf(d.getTime()); };
const monthsBefore = (iso: string, n: number) => { const d = parseDay(iso); return isoOf(new Date(d.getFullYear(), d.getMonth() - n + 1, 1).getTime()); };

export default function AccountsPage() {
  const router = useRouter();
  const me = useLoad(getMeOnce);                    // a 401 sends the browser to /login
  const asOf = me.data?.as_of_date;
  const profile = useLoad(getMyProfile);
  const forecast = useLoad(getMyForecast);
  const status = useLoad(getMyStatus);
  const recurring = useLoad(getMyRecurring);
  // transactions need the as-of date, so they start once /api/me answers
  const spend = useLoad(() => getMeOnce().then((m) => fetchAll({ start: monthsBefore(m.as_of_date, 3), flow_class: "expense" })));
  const recent = useLoad(() => getMeOnce().then((m) => fetchAll({ start: daysBefore(m.as_of_date, 59) })));

  const signOut = async () => { try { await logout(); } finally { router.replace("/login"); } };

  return (
    <>
      <Header name={me.data ? `${me.data.first_name} ${me.data.last_name}` : ""} asOf={asOf ?? ""} onSignOut={signOut} />
      <main className="mx-auto w-full max-w-[1120px] flex-1 px-4 pb-16 sm:px-6">
        <h1 className="sr-only">Accounts</h1>
        {me.error && <p role="alert" className="py-8 text-[15px]">Your accounts could not be loaded. Refresh the page to try again.</p>}
        {!me.data && !me.error && <div className="py-10"><Skeleton className="h-16 w-full" /></div>}
        {me.data && <div className="fade-in"><SummaryRow me={me.data} status={status.data} /></div>}
        <div className="grid gap-x-12 gap-y-10 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
          <div className="space-y-10">
            <AdvisorCard />
            <CashForecast load={forecast} />
            <AccountGroups load={profile} />
          </div>
          <div className="space-y-10">
            <Recurring load={recurring} />
            <Spending txs={spend} profile={profile} />
          </div>
        </div>
        <div className="mt-10"><Transactions load={recent} /></div>
      </main>
    </>
  );
}
