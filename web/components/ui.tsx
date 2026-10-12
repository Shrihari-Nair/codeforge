"use client";
import { useState, type ReactNode } from "react";
import { CountUp } from "./fx";

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

type Cell = { t: string; c: "a" | "d" | "m" | "h" } | null;
type Row = { l: Cell; r: Cell; full?: boolean };

/** Pairs each run of removed lines with the following run of added lines, side by side. */
function splitRows(patch: string): Row[] {
  const rows: Row[] = [];
  let del: string[] = [];
  let add: string[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(del.length, add.length); i++)
      rows.push({ l: i < del.length ? { t: del[i], c: "d" } : null, r: i < add.length ? { t: add[i], c: "a" } : null });
    del = [];
    add = [];
  };
  for (const l of patch.split("\n")) {
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ")) {
      flush();
      rows.push({ l: { t: l, c: "m" }, r: null, full: true });
    } else if (l.startsWith("@@")) {
      flush();
      rows.push({ l: { t: l, c: "h" }, r: null, full: true });
    } else if (l.startsWith("-")) del.push(l);
    else if (l.startsWith("+")) add.push(l);
    else {
      flush();
      rows.push({ l: { t: l || " ", c: "m" }, r: { t: l || " ", c: "m" } });
    }
  }
  flush();
  return rows;
}

function Unified({ patch }: { patch: string }) {
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

export function DiffView({ patch }: { patch: string }) {
  const [mode, setMode] = useState<"split" | "unified">("split");
  const stat = patch.split("\n").reduce((n, l) => ({ a: n.a + +(l.startsWith("+") && !l.startsWith("+++")), d: n.d + +(l.startsWith("-") && !l.startsWith("---")) }), { a: 0, d: 0 });
  return (
    <div>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <span className="mono"><span style={{ color: "#7ee787" }}>+{stat.a}</span> <span style={{ color: "#ffa198" }}>-{stat.d}</span></span>
        <div className="tabs" style={{ margin: 0 }}>
          {(["split", "unified"] as const).map((m) => (
            <button key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>{m}</button>
          ))}
        </div>
      </div>
      {mode === "unified" ? (
        <Unified patch={patch} />
      ) : (
        <div className="diff split">
          {splitRows(patch).map((r, i) =>
            r.full ? (
              <pre key={i} className={`full ${r.l!.c}`}>{r.l!.t}</pre>
            ) : (
              <div key={i} className="srow">
                <pre className={r.l?.c ?? "e"}>{r.l?.t ?? " "}</pre>
                <pre className={r.r?.c ?? "e"}>{r.r?.t ?? " "}</pre>
              </div>
            ),
          )}
        </div>
      )}
    </div>
  );
}

export const Gates = ({ gates }: { gates?: Record<string, boolean> }) => (
  <ul className="gates">
    {Object.entries(gates ?? {}).map(([k, v], i) => (
      <li key={k} className={`gate ${v ? "p" : "f"}`} style={{ ["--i" as string]: i }}>
        <span className={v ? "tick" : "cross"} aria-hidden />
        <span className="sr">{v ? "passed" : "failed"}</span>
        {k.replace(/_/g, " ")}
      </li>
    ))}
  </ul>
);

export const Bench = ({ b }: { b?: { baseline: { p95_ms: number; db_queries: number }; candidate: { p95_ms: number; db_queries: number }; p95_change_pct: number } }) =>
  b ? (
    <div className="bench">
      <div>
        <span className="dim">p95 latency</span>
        <p>
          <s><CountUp to={b.baseline.p95_ms} dec={1} /></s> <i>→</i> <b><CountUp to={b.candidate.p95_ms} dec={1} /></b> <small>ms</small>
        </p>
        <span className={`delta ${b.p95_change_pct <= 0 ? "good" : "bad"}`}>{b.p95_change_pct.toFixed(0)}%</span>
      </div>
      <div>
        <span className="dim">db queries</span>
        <p>
          <s>{b.baseline.db_queries}</s> <i>→</i> <b><CountUp to={b.candidate.db_queries} /></b>
        </p>
      </div>
    </div>
  ) : null;
