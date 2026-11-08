"use client";
import { useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import { ApiError, demoPersonas, login, type DemoPersona } from "@/lib/api";

export default function LoginForm({ passwordHint }: { passwordHint?: string }) {
  const router = useRouter();
  const emailId = useId();
  const passwordId = useId();
  const errorId = useId();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [personas, setPersonas] = useState<DemoPersona[]>([]);

  useEffect(() => {
    demoPersonas().then(setPersonas).catch(() => {});                       // 404 outside demo mode: no buttons
  }, [router]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace("/accounts");
    } catch (err) {
      setPassword("");
      setError(err instanceof ApiError && typeof err.detail === "string" ? err.detail : "Could not sign in. Try again.");
      setBusy(false);
    }
  };

  return (
    <main className="mx-auto flex w-full max-w-[420px] flex-1 flex-col justify-center px-4 py-12 sm:px-6">
      <div className="rise-in">
        <div className="text-[18px] font-semibold tracking-tight text-navy">Guidestone</div>
        <h1 className="mt-8 text-[32px] font-medium leading-tight tracking-tight">Sign in</h1>
        <p className="mt-2 text-[16px] text-ink-2">to see your accounts, cash forecast and net worth.</p>

        <form onSubmit={submit} className="mt-8 space-y-5" aria-describedby={error ? errorId : undefined}>
          <div>
            <label htmlFor={emailId} className="label">Email</label>
            <input
              id={emailId} type="email" name="email" autoComplete="username" required autoFocus
              value={email} onChange={(e) => setEmail(e.target.value)} aria-invalid={!!error}
              className="mt-2 w-full rounded-[6px] border border-line-strong bg-surface px-3 py-2.5 text-[16px] outline-none focus-visible:border-navy"
            />
          </div>
          <div>
            <label htmlFor={passwordId} className="label">Password</label>
            <input
              id={passwordId} type="password" name="password" autoComplete="current-password" required maxLength={256}
              value={password} onChange={(e) => setPassword(e.target.value)} aria-invalid={!!error}
              className="mt-2 w-full rounded-[6px] border border-line-strong bg-surface px-3 py-2.5 text-[16px] outline-none focus-visible:border-navy"
            />
          </div>
          {error && <p id={errorId} role="alert" className="text-[14px] text-poor">{error}</p>}
          <button
            type="submit" disabled={busy}
            className="press w-full rounded-[6px] bg-orange px-4 py-2.5 text-[16px] font-semibold text-navy disabled:opacity-60"
          >
            {busy ? "Signing in…" : "Sign in"}
          </button>
        </form>

        {personas.length > 0 && (
          <section className="mt-10 border-t border-line pt-6" aria-labelledby="demo-label">
            <p id="demo-label" className="label">Demo customers</p>
            <p className="mt-2 text-[14px] text-ink-2">Pick an email to fill it in, then enter the demo password{passwordHint ? <>: <span className="num text-ink">{passwordHint}</span></> : "."}</p>
            <ul className="mt-3 space-y-2">
              {personas.map((p) => (
                <li key={p.customer_id}>
                  <button
                    type="button"
                    onClick={() => { setEmail(p.email); setError(null); document.getElementById(passwordId)?.focus(); }}
                    className="press row-hover flex w-full items-center justify-between rounded-[6px] border border-line bg-surface px-3 py-2 text-left text-[14px]"
                  >
                    <span className="num">{p.email}</span>
                    <span className="text-ink-2">{p.first_name} {p.last_name}</span>
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </main>
  );
}
