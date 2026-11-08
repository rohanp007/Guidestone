"use client";
import { useEffect, useMemo, useState } from "react";
import { ApiError, listFormulas, runFormula, type FormulaInfo } from "@/lib/api";
import { Json } from "./ui";

type Schema = { type?: string; enum?: unknown[]; anyOf?: Schema[]; default?: unknown; description?: string; title?: string };
type Props = Record<string, Schema>;

/** Collapse a nullable `anyOf` to its non-null branch so the form sees one type. */
const resolve = (s: Schema): Schema => {
  if (!s.anyOf) return s;
  const non = s.anyOf.filter((x) => x.type !== "null");
  return { ...s, ...(non[0] ?? {}), anyOf: undefined };
};

/** 422 -> {field: message}; the field is the last string in each error's `loc`. */
function fieldErrors(detail: unknown): Record<string, string> {
  const out: Record<string, string> = {};
  if (Array.isArray(detail))
    for (const e of detail) {
      const loc: unknown[] = e?.loc ?? [];
      const key = [...loc].reverse().find((x) => typeof x === "string") as string | undefined;
      if (key) out[key] = (out[key] ? out[key] + "; " : "") + (e.msg ?? "invalid");
    }
  return out;
}

export default function FormulaRunner({ id }: { id: string }) {
  const [formulas, setFormulas] = useState<FormulaInfo[]>([]);
  const [name, setName] = useState("");
  const [values, setValues] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false);
  const [out, setOut] = useState<{ result: unknown; computeMs: number; cached: boolean } | null>(null);
  const [errs, setErrs] = useState<Record<string, string>>({});
  const [msg, setMsg] = useState<string | null>(null);

  useEffect(() => {
    listFormulas().then((f) => { setFormulas(f); setName(f[0]?.name ?? ""); }).catch((e) => setMsg(String(e)));
  }, []);

  const formula = formulas.find((f) => f.name === name);
  const props = useMemo(() => ((formula?.args_schema as { properties?: Props })?.properties ?? {}), [formula]);

  async function run() {
    setBusy(true); setErrs({}); setMsg(null); setOut(null);
    try {
      const args: Record<string, unknown> = {};
      for (const [k, raw] of Object.entries(values)) {
        if (raw === "") continue;
        const t = resolve(props[k] ?? {}).type;
        if (t === "integer" || t === "number") args[k] = Number(raw);
        else if (t === "boolean") args[k] = raw === "true";
        else if (t === "array" || t === "object") args[k] = JSON.parse(raw);
        else args[k] = raw;
      }
      setOut(await runFormula(id, name, args));
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        const fe = fieldErrors(e.detail);
        setErrs(fe);
        if (!Object.keys(fe).length) setMsg(`Invalid arguments: ${JSON.stringify(e.detail)}`);
      } else if (e instanceof ApiError && e.status === 501) {
        const m = typeof e.detail === "string" ? e.detail.match(/type '([^']+)'/) : null;
        setMsg(`Not implemented yet: ${m ? m[1] : e.detail}`);
      } else if (e instanceof SyntaxError) {
        setMsg(`A JSON field is not valid JSON: ${e.message}`);
      } else {
        setMsg(e instanceof ApiError ? `Error ${e.status}: ${JSON.stringify(e.detail)}` : String(e));
      }
    } finally { setBusy(false); }
  }

  return (
    <div className="text-sm space-y-3">
      <select className="border px-2 py-1 bg-white text-gray-900" value={name} onChange={(e) => { setName(e.target.value); setValues({}); setErrs({}); setMsg(null); setOut(null); }}>
        {formulas.map((f) => <option key={f.name} value={f.name}>{f.category} / {f.name}</option>)}
      </select>
      {formula && <p className="text-gray-600">{formula.description}</p>}
      <div className="grid gap-2 max-w-xl">
        {Object.entries(props).map(([k, raw]) => {
          const s = resolve(raw);
          const ph = s.default !== undefined && s.default !== null ? `default ${JSON.stringify(s.default)}` : "";
          const common = { className: "border px-2 py-1 w-full bg-white text-gray-900", value: values[k] ?? "", id: `f-${k}` };
          const set = (v: string) => setValues((p) => ({ ...p, [k]: v }));
          return (
            <label key={k} className="block">
              <span className="font-mono">{k}</span> <span className="text-gray-500">{s.type}{s.description ? ` – ${s.description}` : ""}</span>
              {s.enum ? (
                <select {...common} onChange={(e) => set(e.target.value)}><option value="">(default)</option>{s.enum.map((o) => <option key={String(o)}>{String(o)}</option>)}</select>
              ) : s.type === "boolean" ? (
                <select {...common} onChange={(e) => set(e.target.value)}><option value="">(default {String(s.default)})</option><option>true</option><option>false</option></select>
              ) : s.type === "array" || s.type === "object" ? (
                <textarea {...common} rows={3} placeholder={`JSON${ph ? `, ${ph}` : ""}, e.g. [{"type":"lump_sum","amount":-500,"day":"2026-12-01"}]`} onChange={(e) => set(e.target.value)} />
              ) : (
                <input {...common} type={s.type === "integer" || s.type === "number" ? "number" : "text"} step="any" placeholder={ph} onChange={(e) => set(e.target.value)} />
              )}
              {errs[k] && <span className="text-red-600 block">{errs[k]}</span>}
            </label>
          );
        })}
      </div>
      <button onClick={run} disabled={busy || !name} className="border px-3 py-1 bg-gray-900 text-white disabled:opacity-50">{busy ? "Running…" : "Run"}</button>
      {msg && <p className="text-amber-700 font-semibold">{msg}</p>}
      {out && (
        <div>
          <p className="font-mono mb-1">X-Compute-Ms: {out.cached ? "cached" : Number.isFinite(out.computeMs) ? `${out.computeMs}ms` : "–"}</p>
          <Json value={out.result} />
        </div>
      )}
    </div>
  );
}
