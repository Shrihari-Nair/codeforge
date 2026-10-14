"use client";
import { useEffect, useState, type ReactNode } from "react";
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

type Cell = { t: string; c: "a" | "d" | "m" | "h"; ix: number } | null;
type Row = { l: Cell; r: Cell; full?: boolean; ix: number };
export type LineComment = { line: string; text: string };

/** Pairs each run of removed lines with the following run of added lines, side by side.
 *  `ix` is the line's index in the patch, which is what a review comment attaches to. */
function splitRows(patch: string): Row[] {
  const rows: Row[] = [];
  let del: { t: string; ix: number }[] = [];
  let add: { t: string; ix: number }[] = [];
  const flush = () => {
    for (let i = 0; i < Math.max(del.length, add.length); i++) {
      const l = i < del.length ? ({ ...del[i], c: "d" } as const) : null;
      const r = i < add.length ? ({ ...add[i], c: "a" } as const) : null;
      rows.push({ l, r, ix: (r ?? l)!.ix });
    }
    del = [];
    add = [];
  };
  patch.split("\n").forEach((l, ix) => {
    if (l.startsWith("+++") || l.startsWith("---") || l.startsWith("diff ") || l.startsWith("index ")) {
      flush();
      rows.push({ l: { t: l, c: "m", ix }, r: null, full: true, ix });
    } else if (l.startsWith("@@")) {
      flush();
      rows.push({ l: { t: l, c: "h", ix }, r: null, full: true, ix });
    } else if (l.startsWith("-")) del.push({ t: l, ix });
    else if (l.startsWith("+")) add.push({ t: l, ix });
    else {
      flush();
      rows.push({ l: { t: l || " ", c: "m", ix }, r: { t: l || " ", c: "m", ix }, ix });
    }
  });
  flush();
  return rows;
}

const lineClass = (l: string) =>
  l.startsWith("+++") || l.startsWith("---") ? "m" : l.startsWith("+") ? "a" : l.startsWith("-") ? "d" : l.startsWith("@@") ? "h" : "m";

/** Diff viewer. With `onComments`, every line gets a "+" to attach a review comment; the
 *  comments are handed to the parent so they travel with the approve/reject decision. */
export function DiffView({ patch, onComments }: { patch: string; onComments?: (c: LineComment[]) => void }) {
  const [mode, setMode] = useState<"split" | "unified">("split");
  const [notes, setNotes] = useState<Record<number, string>>({});
  const [open, setOpen] = useState<number | null>(null);
  const [draft, setDraft] = useState("");
  const lines = patch.split("\n");
  const stat = lines.reduce((n, l) => ({ a: n.a + +(l.startsWith("+") && !l.startsWith("+++")), d: n.d + +(l.startsWith("-") && !l.startsWith("---")) }), { a: 0, d: 0 });

  useEffect(() => {
    onComments?.(Object.entries(notes).map(([ix, text]) => ({ line: lines[+ix] ?? "", text })));
  }, [notes]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = (ix: number) => {
    const text = draft.trim();
    setNotes((n) => {
      const next = { ...n };
      if (text) next[ix] = text;
      else delete next[ix];
      return next;
    });
    setOpen(null);
  };
  const extras = (ix: number) =>
    onComments && (
      <>
        <button type="button" className="lplus" aria-label="Comment on this line" onClick={() => { setOpen(ix); setDraft(notes[ix] ?? ""); }}>+</button>
        {notes[ix] && open !== ix && <div className="lcom"><span>{notes[ix]}</span><button type="button" onClick={() => setNotes((n) => { const x = { ...n }; delete x[ix]; return x; })}>remove</button></div>}
        {open === ix && (
          <form className="lform" onSubmit={(e) => { e.preventDefault(); save(ix); }}>
            <input autoFocus value={draft} maxLength={300} placeholder="Comment on this line" aria-label="Comment" onChange={(e) => setDraft(e.target.value)} />
            <button type="submit" className="primary">Add</button>
            <button type="button" onClick={() => setOpen(null)}>Cancel</button>
          </form>
        )}
      </>
    );
  const count = Object.keys(notes).length;

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
        <div className="diff">
          {lines.map((l, i) => (
            <div key={i} className="crow">
              <pre className={lineClass(l)}>{l || " "}</pre>
              {extras(i)}
            </div>
          ))}
        </div>
      ) : (
        <div className="diff split">
          {splitRows(patch).map((r, i) =>
            r.full ? (
              <div key={i} className="crow"><pre className={`full ${r.l!.c}`}>{r.l!.t}</pre>{extras(r.ix)}</div>
            ) : (
              <div key={i} className="crow">
                <div className="srow">
                  <pre className={r.l?.c ?? "e"}>{r.l?.t ?? " "}</pre>
                  <pre className={r.r?.c ?? "e"}>{r.r?.t ?? " "}</pre>
                </div>
                {extras(r.ix)}
              </div>
            ),
          )}
        </div>
      )}
      {onComments && (
        <p className="dim" style={{ margin: "10px 0 0", fontSize: 13 }}>
          {count ? `${count} line comment${count > 1 ? "s" : ""} will be sent with your decision.` : "Hover a line and press + to leave a comment. Comments are sent with your decision."}
        </p>
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

// A candidate that is more than 10% above the baseline is a regression: show it as one.
const worse = (cand: number, base: number) => (cand > base * 1.1 ? "bad" : "");

export const Bench = ({ b }: { b?: { baseline: { p95_ms: number; db_queries: number }; candidate: { p95_ms: number; db_queries: number }; p95_change_pct: number } }) =>
  b ? (
    <div className="bench">
      <div>
        <span className="dim">p95 latency</span>
        <p>
          <s><CountUp to={b.baseline.p95_ms} dec={1} /></s> <i>→</i> <b className={worse(b.candidate.p95_ms, b.baseline.p95_ms)}><CountUp to={b.candidate.p95_ms} dec={1} /></b> <small>ms</small>
        </p>
        <span className={`delta ${b.p95_change_pct <= 0 ? "good" : "bad"}`}>{b.p95_change_pct.toFixed(0)}%</span>
      </div>
      <div>
        <span className="dim">db queries</span>
        <p>
          <s>{b.baseline.db_queries}</s> <i>→</i> <b className={worse(b.candidate.db_queries, b.baseline.db_queries)}><CountUp to={b.candidate.db_queries} /></b>
        </p>
      </div>
    </div>
  ) : null;
