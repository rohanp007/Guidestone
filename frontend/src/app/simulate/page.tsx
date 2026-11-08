import type { Metadata } from "next";
import SimulateLanding from "@/components/shell/SimulateLanding";

export const metadata: Metadata = { title: "Simulate · Guidestone" };

export default function SimulatePage() {
  return <SimulateLanding />;
}
