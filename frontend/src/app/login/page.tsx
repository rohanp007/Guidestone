import { Suspense } from "react";
import { connection } from "next/server";
import LoginForm from "@/components/accounts/LoginForm";

/** The password hint is read from the environment on each request (never hardcoded, never built into the bundle). */
async function Form() {
  await connection();
  return <LoginForm passwordHint={process.env.DEMO_PASSWORD_HINT || undefined} />;
}

export default function LoginPage() {
  return <Suspense fallback={null}><Form /></Suspense>;
}
