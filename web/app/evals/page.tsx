"use client";
import { useEffect, useState } from "react";
import { api, EvalRun, EvalSummary } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import { Badge, ErrorNote } from "@/components/ui";
import { CountUp } from "@/components/fx";
import { Deltas, pct, pretty, TemplateBars, TemplateTable } from "@/components/EvalCharts";

const nice = (id: string) => id.split("_").slice(1).join("_");

export default function Evals() {
  const { data: list, error } = usePoll(() => api.evals(), 60000);
  const [pick, setPick] = useState<string | null>(null);
  const [run, setRun] = useState<EvalRun | null>(null);
  const [runErr, setRunErr] = useState<string | null>(null);
  const [view, setView] = useState<"chart" | "table">("chart");
  const id = pick ?? (list as EvalSummary[] | null)?.find((r) => r.meta.llm === "gemini")?.id ?? list?.[0]?.id ?? null;

  useEffect(() => {
    if (!id) return;
    let live = true;
    api.evalRun(id).then((r) => live && (setRun(r), setRunErr(null))).catch((e) => live && setRunErr(String(e.message ?? e)));
    return () => {
      live = false;
    };
  }, [id]);

  const o = run?.id === id ? run.aggregate.overall : null;
  const tiles = o && [
    { k: "Task success", v: o.success_rate.mean * 100, d: 0, u: "%", s: `± ${(o.success_rate.stderr * 100).toFixed(0)} pts over ${o.tasks} tasks` },
    { k: "Root cause correct", v: o.root_cause_accuracy.mean * 100, d: 0, u: "%", s: `± ${(o.root_cause_accuracy.stderr * 100).toFixed(0)} pts` },
    { k: "Recovered after a failure", v: o.recovery_rate.mean * 100, d: 0, u: "%", s: o.recovery_rate.n ? `${o.recovery_rate.n} runs had a failed attempt` : "no failed attempts in this run", na: !o.recovery_rate.n },
    { k: "Cost per task", v: o.avg_cost_usd.mean, d: 4, p: "$", s: `${o.avg_llm_calls.mean.toFixed(0)} LLM calls on average` },
    { k: "Time per task", v: o.avg_latency_s.mean, d: 1, u: " s", s: `${o.avg_agent_steps.mean.toFixed(0)} agent steps on average` },
  ];

  return (
    <>
      <h1>Evals</h1>
      <p className="lead" style={{ margin: "4px 0 24px" }}>
        Each run replays seeded latency regressions with a known root cause and scores the result against ground truth.
      </p>
      <ErrorNote e={error} />
      {list && !list.length && (
        <section className="card"><p className="empty">No eval runs yet. Run <span className="mono">uv run python -m evals.run_evals --suite smoke</span>.</p></section>
      )}

      {list && list.length > 0 && (
        <div className="tabs" role="tablist" aria-label="Eval run">
          {list.map((r) => (
            <button key={r.id} role="tab" aria-selected={r.id === id} className={r.id === id ? "on" : ""} onClick={() => setPick(r.id)}>
              {nice(r.id)}
            </button>
          ))}
        </div>
      )}

      {runErr && <p className="err">Could not load this run: {runErr}</p>}
      {id && !o && !runErr && (
        <div className="stats five">{[0, 1, 2, 3, 4].map((i) => <div key={i} className="stat"><div className="skel" style={{ height: 40, width: "60%" }} /></div>)}</div>
      )}

      {run && o && tiles && run.id === id && (
        <>
          <p className="row dim" style={{ marginBottom: 16 }}>
            <span className="badge">{run.meta.llm}{run.meta.model ? ` · ${run.meta.model}` : ""}</span>
            {run.meta.llm !== "gemini" && <span className="badge">scenario: {pretty(run.meta.scenario)}</span>}
            {run.meta.break !== "none" && <span className="badge s-failed">fault injected: {pretty(run.meta.break)}</span>}
            <span>{o.tasks} tasks, {run.meta.trials} trial{run.meta.trials > 1 ? "s" : ""} each</span>
          </p>

          <section className="stats five" aria-label="Headline numbers">
            {tiles.map((t, i) => (
              <div key={t.k} className="stat" style={{ ["--i" as string]: i }}>
                <b>{t.na ? "n/a" : <>{t.p}<CountUp key={id} to={t.v} dec={t.d} />{t.u}</>}</b>
                <span>{t.k}</span>
                <small>{t.s}</small>
              </div>
            ))}
          </section>

          {run.vs_baseline && (
            <section className="card" style={{ ["--i" as string]: 2 }}>
              <h2>Compared with an earlier run</h2>
              <p className="dim" style={{ margin: "0 0 12px" }}>
                {run.meta.break !== "none"
                  ? `This run broke the ${pretty(run.meta.break)} logic on purpose, to check the evals notice.`
                  : "Change since the earlier run. Code, prompts and graders may differ between the two, so this is not a controlled comparison."}{" "}
                Compared with: <span className="mono">{nice(run.vs_baseline.file.split("/").pop()!.replace(".json", ""))}</span>
              </p>
              <Deltas deltas={run.vs_baseline.deltas} />
            </section>
          )}

          <section className="card" style={{ ["--i" as string]: 3 }}>
            <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
              <h2 style={{ margin: 0 }}>Success rate by scenario</h2>
              <div className="tabs" style={{ margin: 0 }}>
                {(["chart", "table"] as const).map((v) => (
                  <button key={v} className={view === v ? "on" : ""} onClick={() => setView(v)}>{v}</button>
                ))}
              </div>
            </div>
            {view === "chart" ? <TemplateBars data={run.aggregate.by_template} /> : <TemplateTable data={run.aggregate.by_template} />}
          </section>

          <details className="card" style={{ ["--i" as string]: 4 }}>
            <summary><h2 style={{ display: "inline" }}>All {run.runs.length} tasks</h2></summary>
            <div className="tbl" style={{ marginTop: 12 }}>
              <table>
                <thead><tr><th>Task</th><th>Scenario</th><th>Status</th><th>Outcome</th><th>Time</th><th>Cost</th></tr></thead>
                <tbody>
                  {run.runs.map((r) => (
                    <tr key={r.task_id}>
                      <td className="mono">{r.task_id}</td>
                      <td>{pretty(r.template)}</td>
                      <td>{r.status && <Badge s={r.status} />}</td>
                      <td>{r.success ? "success" : "not successful"}</td>
                      <td>{r.latency_s != null ? `${r.latency_s.toFixed(0)} s` : "n/a"}</td>
                      <td>{r.cost_usd != null ? `$${r.cost_usd.toFixed(3)}` : "n/a"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </details>
        </>
      )}
    </>
  );
}
