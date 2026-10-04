"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Badge, Card, ErrorNote } from "@/components/ui";

const DEMO_GOAL =
  "Find why checkout latency increased, fix it, verify the fix, and prepare a PR. Do not merge without my approval.";
const CHANGE_GOAL = "Update the ws (websocket) dependency to the latest version, check everywhere the change must be made, and raise a PR.";

export default function Home() {
  const router = useRouter();
  const { data: meta } = usePoll(() => api.meta(), 60000);
  const [kind, setKind] = useState<"change" | "regression" | null>(null);
  const [goal, setGoal] = useState<string | null>(null);
  const effKind = kind ?? (meta?.target_repo ? "change" : "regression");
  const effGoal = goal ?? (effKind === "change" ? CHANGE_GOAL : DEMO_GOAL);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const { data: rows, error } = usePoll(() => api.tasks(), 3000);
  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const { task_id } = await api.createTask(effGoal, effKind);
      router.push(`/tasks/${task_id}`);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };
  return (
    <>
      <h1>Tasks</h1>
      <Card title="New task">
        <div className="row" style={{ marginBottom: 8 }}>
          <span className="dim">Target</span>
          <select
            value={effKind}
            onChange={(e) => {
              setKind(e.target.value as "change" | "regression");
              setGoal(null);
            }}
          >
            {meta?.target_repo && <option value="change">Change request on {meta.target_repo} (real repo → real PR)</option>}
            <option value="regression">Latency regression (seeded demo repo)</option>
          </select>
        </div>
        <textarea rows={3} value={effGoal} onChange={(e) => setGoal(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <button className="primary" disabled={busy || !effGoal.trim()} onClick={submit}>
            Submit
          </button>
          {err && <span className="err">{err}</span>}
        </div>
      </Card>
      <ErrorNote e={error} />
      <Card title="All tasks">
        <table>
          <thead>
            <tr><th>Task</th><th>Goal</th><th>Status</th><th>Attempts</th><th>Created</th></tr>
          </thead>
          <tbody>
            {(rows ?? []).map((t) => (
              <tr key={t.task_id} className="click" onClick={() => router.push(`/tasks/${t.task_id}`)}>
                <td className="mono"><Link href={`/tasks/${t.task_id}`}>{t.task_id}</Link></td>
                <td>{t.goal.slice(0, 90)}</td>
                <td><Badge s={t.status} /></td>
                <td>{t.attempts}</td>
                <td className="dim">{new Date(t.created_at).toLocaleString()}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {rows && !rows.length && <p className="dim">No tasks yet.</p>}
      </Card>
    </>
  );
}
