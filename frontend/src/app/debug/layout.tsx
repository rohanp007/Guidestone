import { notFound } from "next/navigation";
import { connection } from "next/server";

/** /debug (and /debug/[id]) exist only when DEMO_MODE=true. */
export default async function DebugLayout({ children }: { children: React.ReactNode }) {
  await connection();
  if (process.env.DEMO_MODE !== "true") notFound();
  return children;
}
