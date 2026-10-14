"use client";
import { useState } from "react";
import { Approval, api } from "@/lib/api";
import { useActor } from "@/lib/hooks";
import { useMe } from "@/lib/auth";
import { Badge, Bench, DiffView, Gates, type LineComment } from "./ui";

export default function ApprovalCard({ a, onDone }: { a: Approval; onDone: () => void }) {
  const [actor, setActor] = useActor();
  const authed = useMe().authRequired;
  const [feedback, setFeedback] = useState("");
  const [lines, setLines] = useState<LineComment[]>([]);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const decide = async (d: "approve" | "reject") => {
    setBusy(true);
    setErr(null);
    try {
      // Free text plus any per-line comments, quoted so the planner sees what each one refers to.
      const notes = lines.map((c) => `On \`${c.line.replace(/^[-+ ]/, "").trim().slice(0, 80)}\`: ${c.text}`);
      await api.decide(a.id, d, actor || "anonymous", [feedback.trim(), ...notes].filter(Boolean).join("\n").slice(0, 2000));
      onDone();
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  const ev = a.evidence ?? {};
  const pending = a.status === "pending";
  return (
    <section className={`card ${pending ? "approval beam" : ""}`}>
      <div className="row">
        <h2 style={{ margin: 0 }}>Approval request</h2>
        <Badge s={a.status} />
        <span className="dim mono">{a.id}</span>
        <span className="dim">expires {new Date(a.expires_at).toLocaleString()}</span>
      </div>
      <p>
        <b>{a.tool}</b>: {a.arguments.title}{" "}
        <span className="dim">
          ({a.arguments.head} into {a.arguments.base})
        </span>
      </p>
      <p className="dim">{a.rationale}</p>
      <h2>Verifier gates</h2>
      <Gates gates={ev.gates} />
      <h2 style={{ marginTop: 10 }}>Benchmark</h2>
      <Bench b={ev.benchmark} />
      {ev.root_cause && (
        <p>
          <span className="dim">Root cause:</span> {ev.root_cause.file}: {ev.root_cause.summary}
        </p>
      )}
      {ev.patch && (
        <>
          <h2 style={{ marginTop: 10 }}>Diff</h2>
          <DiffView patch={ev.patch} onComments={pending ? setLines : undefined} />
        </>
      )}
      {pending ? (
        <div style={{ marginTop: 12 }}>
          <div className="row" style={{ marginBottom: 8 }}>
            <span className="dim">Acting as</span>
            <input value={actor} onChange={(e) => setActor(e.target.value)} size={14} readOnly={authed} title={authed ? "You are signed in; this name is recorded" : undefined} />
          </div>
          <textarea rows={2} placeholder="Feedback (optional)" value={feedback} onChange={(e) => setFeedback(e.target.value)} />
          <div className="row" style={{ marginTop: 8 }}>
            <button className="ok" disabled={busy} onClick={() => decide("approve")}>
              Approve
            </button>
            <button className="no" disabled={busy} onClick={() => decide("reject")}>
              Reject
            </button>
            {err && <span className="err">{err}</span>}
          </div>
        </div>
      ) : (
        <p className="dim">Decided by {a.actor ?? "unknown"}.</p>
      )}
    </section>
  );
}
