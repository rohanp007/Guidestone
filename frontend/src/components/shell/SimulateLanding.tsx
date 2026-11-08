"use client";
import Link from "next/link";
import PageShell from "./PageShell";

/** A client component, because PageShell takes a render function and functions cannot cross from a server page. */
export default function SimulateLanding() {
  return (
    <PageShell active="simulate" title="Simulate">
      {() => (
        <div className="fade-in py-8 sm:py-10">
          <p className="label">Simulate</p>
          <p className="mt-2 max-w-[56ch] text-[20px] font-medium leading-snug text-navy">Try a decision against your own numbers before you make it.</p>
          <ul className="mt-8 grid gap-4 sm:grid-cols-2">
            <li>
              <Link href="/simulate/payoff" className="press row-hover block rounded-[6px] border border-line-strong bg-surface p-5">
                <span className="text-[18px] font-medium">Pay off debt</span>
                <span className="mt-1 block text-[15px] text-ink-2">See whether to pay down your cards and loans, which order to do it in, and what it does to your cash.</span>
                <span className="mt-3 block text-[14px] font-medium text-navy">Start →</span>
              </Link>
            </li>
            <li>
              <Link href="/simulate/buy" className="press row-hover block rounded-[6px] border border-line-strong bg-surface p-5">
                <span className="text-[18px] font-medium">Buy</span>
                <span className="mt-1 block text-[15px] text-ink-2">Find cars that fit your budget, then see whether to pay cash, finance, or a mix, and what it does to your cash.</span>
                <span className="mt-3 block text-[14px] font-medium text-navy">Start →</span>
              </Link>
            </li>
          </ul>
        </div>
      )}
    </PageShell>
  );
}
