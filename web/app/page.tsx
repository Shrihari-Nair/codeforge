"use client";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, TaskRow } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Badge, ErrorNote } from "@/components/ui";
import { CountUp, Scramble } from "@/components/fx";
import Pipeline from "@/components/Pipeline";

const DEMO_GOAL =
  "Find why checkout latency increased, fix it, verify the fix, and prepare a PR. Do not merge without my approval.";
const CHANGE_GOAL = "Update the ws (websocket) dependency to the latest version, check everywhere the change must be made, and raise a PR.";

const GROUPS = {
  running: ["queued", "running", "planning", "executing", "verifying"],
  awaiting: ["awaiting_approval"],
  done: ["completed"],
  failed: ["failed", "escalated", "cancelled"],
} as const;
type Filter = "all" | keyof typeof GROUPS;
const inGroup = (t: TaskRow, g: keyof typeof GROUPS) => (GROUPS[g] as readonly string[]).includes(t.status);
const orb = (s: string) =>
  (GROUPS.running as readonly string[]).includes(s) ? "run" : s === "completed" ? "ok" : s === "awaiting_approval" ? "warn" : (GROUPS.failed as readonly string[]).includes(s) ? "bad" : "";

export default function Home() {
  const router = useRouter();
  const { data: meta } = usePoll(() => api.meta(), 60000);
  const [kind, setKind] = useState<"change" | "regression" | null>(null);
  const [goal, setGoal] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("all");
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
  const latest = rows?.[0]?.task_id;
  const { data: last } = usePoll(() => (latest ? api.task(latest) : Promise.resolve(null)), 3000, [latest]);
  const all = rows ?? [];
  const shown = filter === "all" ? all : all.filter((t) => inGroup(t, filter));
  const count = (g: keyof typeof GROUPS) => all.filter((t) => inGroup(t, g)).length;
  const tiles: [string, number][] = [
    ["Total tasks", all.length],
    ["Running now", count("running")],
    ["Awaiting you", count("awaiting")],
    ["Completed", count("done")],
  ];

  return (
    <>
      <section className="hero">
        <div>
          <h1 className="display">
            <Scramble text={"Describe the change.\nAgents ship it."} />
          </h1>
          <p className="lead">Planners, coders and verifiers run your task end to end. Nothing merges until you approve.</p>
          <div className="composer">
            <textarea
              id="composer-goal"
              aria-label="Task goal"
              rows={3}
              value={effGoal}
              onChange={(e) => setGoal(e.target.value)}
              onKeyDown={(e) => (e.metaKey || e.ctrlKey) && e.key === "Enter" && !busy && effGoal.trim() && submit()}
            />
            <div className="bar">
              <select
                aria-label="Target"
                value={effKind}
                onChange={(e) => {
                  setKind(e.target.value as "change" | "regression");
                  setGoal(null);
                }}
              >
                {meta?.target_repo && <option value="change">Change request on {meta.target_repo}</option>}
                <option value="regression">Latency regression (demo repo)</option>
              </select>
              <span className="row">
                <kbd>Ctrl Enter</kbd>
                <button className="primary" disabled={busy || !effGoal.trim()} onClick={submit}>
                  {busy ? "Starting" : "Run task"}
                </button>
              </span>
            </div>
            {err && <p className="err" style={{ margin: "8px 10px 4px" }}>{err}</p>}
          </div>
        </div>

        <div className="panel">
          <h2>Live pipeline</h2>
          {last && <p className="g dim" style={{ margin: "0 0 4px" }}>{last.goal}</p>}
          <Pipeline task={last} />
          <div className="feed" style={{ marginTop: 20 }}>
            {rows === null && [0, 1, 2, 3].map((i) => <div key={i} className="skel" style={{ margin: "16px 0", width: `${90 - i * 12}%` }} />)}
            {all.slice(0, 4).map((t, i) => (
              <Link key={t.task_id} href={`/tasks/${t.task_id}`} style={{ ["--i" as string]: i }}>
                <span className={`orb ${orb(t.status)}`} />
                <span className="g">{t.goal}</span>
                <Badge s={t.status} />
              </Link>
            ))}
            {rows && !rows.length && <p className="empty">No runs yet. Submit a goal and watch it appear here.</p>}
          </div>
        </div>
      </section>

      <ErrorNote e={error} />

      <section className="stats" aria-label="Summary">
        {tiles.map(([label, n], i) => (
          <div key={label} className="stat" style={{ ["--i" as string]: i }}>
            <b><CountUp to={n} /></b>
            <span>{label}</span>
          </div>
        ))}
      </section>

      <section className="card">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>All tasks</h2>
          <div className="tabs" style={{ margin: 0 }} role="tablist">
            {(["all", "running", "awaiting", "done", "failed"] as Filter[]).map((f) => (
              <button key={f} role="tab" aria-selected={filter === f} className={filter === f ? "on" : ""} onClick={() => setFilter(f)}>
                {f}
              </button>
            ))}
          </div>
        </div>
        <div className="tbl">
          <table>
            <thead>
              <tr><th>Task</th><th>Goal</th><th>Status</th><th>Attempts</th><th>Created</th></tr>
            </thead>
            <tbody>
              {shown.map((t) => (
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
        </div>
        {rows && !shown.length && <p className="empty">{filter === "all" ? "No tasks yet." : `No ${filter} tasks.`}</p>}
      </section>
    </>
  );
}
