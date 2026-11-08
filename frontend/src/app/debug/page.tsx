"use client";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { getMe } from "@/lib/api";

/** /debug opens the signed-in customer's own page (a 401 sends the browser to /login). */
export default function DebugIndex() {
  const router = useRouter();
  useEffect(() => { getMe().then((m) => router.replace(`/debug/${m.customer_id}`)).catch(() => {}); }, [router]);
  return <main className="mx-auto max-w-6xl p-4"><p className="label">Loading…</p></main>;
}
