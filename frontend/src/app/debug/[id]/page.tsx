import { Suspense } from "react";
import DebugView from "@/components/debug/DebugView";

export default function DebugPage({ params }: { params: Promise<{ id: string }> }) {
  return (
    <Suspense
      fallback={
        <main className="max-w-6xl mx-auto p-4">
          <h1 className="text-xl font-bold">Debug</h1>
          <p>Loading…</p>
        </main>
      }
    >
      <Debug params={params} />
    </Suspense>
  );
}

async function Debug({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DebugView key={id} id={id} />;
}
