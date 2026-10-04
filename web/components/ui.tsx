"use client";
import type { ReactNode } from "react";

export const Badge = ({ s }: { s: string }) => <span className={`badge s-${s}`}>{s.replace("_", " ")}</span>;
export const Card = ({ title, children }: { title?: string; children: ReactNode }) => (
  <section className="card">
    {title && <h2>{title}</h2>}
    {children}
  </section>
);
export const Json = ({ v }: { v: unknown }) => <pre>{JSON.stringify(v, null, 2)}</pre>;
export const ErrorNote = ({ e }: { e: string | null }) =>
  e ? <p className="err">Backend error: {e}. Is `aeos serve` running?</p> : null;

export function DiffView({ patch }: { patch: string }) {
  return (
    <div className="diff">
      {patch.split("\n").map((l, i) => {
        const c = l.startsWith("+++") || l.startsWith("---") ? "m" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : l.startsWith("@@") ? "h" : "m";
        return (
          <pre key={i} className={c}>
            {l || " "}
          </pre>
        );
      })}
    </div>
  );
}

export const Gates = ({ gates }: { gates?: Record<string, boolean> }) => (
  <div>
    {Object.entries(gates ?? {}).map(([k, v]) => (
      <span key={k} className={`gate ${v ? "p" : "f"}`}>
        {v ? "✓" : "✗"} {k}
      </span>
    ))}
  </div>
);

export const Bench = ({ b }: { b?: { baseline: { p95_ms: number; db_queries: number }; candidate: { p95_ms: number; db_queries: number }; p95_change_pct: number } }) =>
  b ? (
    <div className="kv">
      <span className="dim">p95</span>
      <span>
        {b.baseline.p95_ms.toFixed(1)} ms → {b.candidate.p95_ms.toFixed(1)} ms ({b.p95_change_pct.toFixed(0)}%)
      </span>
      <span className="dim">db queries</span>
      <span>
        {b.baseline.db_queries} → {b.candidate.db_queries}
      </span>
    </div>
  ) : null;
