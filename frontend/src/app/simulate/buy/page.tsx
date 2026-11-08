import type { Metadata } from "next";
import BuyPage from "@/components/buy/BuyPage";

export const metadata: Metadata = { title: "Buy a car · Guidestone" };

export default function Page() {
  return <BuyPage />;
}
