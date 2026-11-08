"use client";
import { useRouter } from "next/navigation";
import type { ReactNode } from "react";
import { getMeOnce, logout, type Me } from "@/lib/api";
import Header, { type NavId } from "@/components/accounts/Header";
import { Skeleton, useLoad } from "@/components/accounts/ui";

/** The signed-in page frame: header (name, as-of date, nav, sign out) and the content column. A 401 sends the browser to /login. */
export default function PageShell({ active, title, children }: { active: NavId; title: string; children: (me: Me) => ReactNode }) {
  const router = useRouter();
  const me = useLoad(getMeOnce);
  const signOut = async () => { try { await logout(); } finally { router.replace("/login"); } };
  return (
    <>
      <Header name={me.data ? `${me.data.first_name} ${me.data.last_name}` : ""} asOf={me.data?.as_of_date ?? ""} onSignOut={signOut} active={active} />
      <main className="mx-auto w-full max-w-[1120px] flex-1 px-4 pb-16 sm:px-6">
        <h1 className="sr-only">{title}</h1>
        {me.error && <p role="alert" className="py-8 text-[15px]">This page could not be loaded. Refresh to try again.</p>}
        {!me.data && !me.error && <div className="py-10"><Skeleton className="h-16 w-full" /></div>}
        {me.data && children(me.data)}
      </main>
    </>
  );
}
