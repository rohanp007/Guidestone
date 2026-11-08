import type { Metadata } from "next";
import AccountsPage from "@/components/accounts/AccountsPage";

export const metadata: Metadata = { title: "Accounts · Guidestone" };

export default function Page() {
  return <AccountsPage />;
}
