"use client";
import type { Me, MyStatus } from "@/lib/api";
import { money0 } from "@/lib/format";
import { Badge, Skeleton, type Tone } from "./ui";

const TONE: Record<MyStatus["status"], Tone> = { on_track: "good", watch: "fair", at_risk: "poor" };

function Figure({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div>
      <dt className="label">{label}</dt>
      <dd className="num mt-1 text-[30px] font-medium leading-tight tracking-tight sm:text-[34px]">{value}</dd>
      {note && <p className="mt-0.5 text-[14px] text-ink-2">{note}</p>}
    </div>
  );
}

export default function SummaryRow({ me, status }: { me: Me; status: MyStatus | null }) {
  const s = me.summary;
  if (!s) return null;
  const negative = s.monthly_surplus < 0;
  return (
    <section aria-label="Summary" className="py-8 sm:py-10">
      <dl className="grid grid-cols-2 gap-x-6 gap-y-8 lg:grid-cols-4">
        <Figure label="Net worth" value={money0(s.net_worth)} />
        <Figure label="Cash on hand" value={money0(s.liquid_cash)} />
        <Figure label="Monthly surplus" value={negative ? `−${money0(-s.monthly_surplus)}` : money0(s.monthly_surplus)}
          note={negative ? `You spend ${money0(-s.monthly_surplus)} more each month than you take home.` : "Left each month after bills and spending."} />
        <div>
          <dt className="label">Cash outlook</dt>
          <dd className="mt-2">
            {status ? <Badge tone={TONE[status.status]}>{status.label}</Badge> : <Skeleton className="h-7 w-32" />}
          </dd>
          {status && <p className="mt-2 text-[14px] text-ink-2">{status.note}</p>}
        </div>
      </dl>
    </section>
  );
}
