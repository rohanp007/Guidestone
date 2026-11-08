import { Suspense } from "react";
import AccountsApp from "@/components/accounts/AccountsApp";

export default function Home() {
  return (
    <Suspense
      fallback={
        <header className="border-b border-line">
          <div className="mx-auto flex max-w-[1120px] items-center px-4 py-3 sm:px-6">
            <div className="text-[18px] font-semibold tracking-tight text-navy">Guidestone</div>
          </div>
        </header>
      }
    >
      <AccountsApp />
    </Suspense>
  );
}
