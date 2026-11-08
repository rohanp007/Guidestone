import type { Metadata } from "next";
import PayoffPage from "@/components/payoff/PayoffPage";

export const metadata: Metadata = { title: "Pay off debt · Guidestone" };

export default function Page() {
  return <PayoffPage />;
}
