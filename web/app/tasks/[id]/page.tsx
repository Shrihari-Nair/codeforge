"use client";
import { use, useState } from "react";
import { api, ToolCall } from "@/lib/api";
import { TERMINAL, useEvents, usePoll } from "@/lib/hooks";
import ApprovalCard from "@/components/ApprovalCard";
import Pipeline from "@/components/Pipeline";
import PlanGraph from "@/components/PlanGraph";
import StatsPanel from "@/components/StatsPanel";
import StepDrilldown from "@/components/StepDrilldown";
import Timeline from "@/components/Timeline";
import { Badge, Bench, Card, DiffView, ErrorNote, Gates } from "@/components/ui";

export default function TaskPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const { data: t, error, refresh } = usePoll(() => api.task(id), 2000, [id], (d) => TERMINAL.includes(d.status));
  const { data: stats } = usePoll(() => api.stats(id), 4000, [id]);
  const { data: meta } = usePoll(() => api.meta(), 60000);
  const { data: approvals, refresh: refreshApprovals } = usePoll(() => api.approvalsFor(id), 2500, [id]);
  const { events, live } = useEvents(id);
  const [ver, setVer] = useState<number | null>(null);
  const [sel, setSel] = useState<string | null>(null);

  if (!t) return <><ErrorNote e={error} /><div className="skel" style={{ width: "40%", height: 28 }} /></>;
  const plans = t.plans ?? [];
  const plan = plans.find((p) => p.version === (ver ?? t.plan_version)) ?? plans[plans.length - 1];
  const mine = (approvals ?? []).filter((a) => a.task_id === id);
  const pending = mine.filter((a) => a.status === "pending");
  const lastV = t.verifications?.[t.verifications.length - 1];
  const langfuse = meta && t.trace_id ? `${meta.langfuse_url.replace(/\/$/, "")}/traces/${t.trace_id}` : null;
  const calls: ToolCall[] = t.tool_calls ?? [];
  const done = plan?.steps.filter((s) => s.status === "done").length ?? 0;
  const pct = t.status === "completed" ? 100 : plan?.steps.length ? Math.round((done / plan.steps.length) * 100) : 0;

  return (
    <>
      <div className="row">
        <h1>{t.task_id}</h1>
        <Badge s={t.status} />
        <span className="badge">{t.kind === "change" ? "change request" : "regression"}</span>
        {langfuse && <a href={langfuse} target="_blank" rel="noreferrer">Open trace in Langfuse ↗</a>}
      </div>
      <p style={{ fontSize: 18, maxWidth: "70ch" }}>{t.goal}</p>
      <p className="dim">{t.status_line}</p>
      <div className={`progress ${TERMINAL.includes(t.status) ? "" : "live"}`} role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100} aria-label="Plan progress">
        <i style={{ width: `${pct}%` }} />
      </div>
      <div className="panel" style={{ margin: "24px 0", padding: "20px 24px" }}>
        <Pipeline task={t} />
      </div>
      {t.error && <p className="err">{t.error}</p>}

      {pending.map((a) => (
        <ApprovalCard key={a.id} a={a} onDone={() => { refresh(); refreshApprovals(); }} />
      ))}

      <div className="grid g2">
        <div>
          <Card title="Plan">
            <div className="tabs">
              {plans.map((p) => (
                <button key={p.version} className={p.version === plan?.version ? "on" : ""} onClick={() => setVer(p.version)}>
                  v{p.version}
                </button>
              ))}
            </div>
            {plan?.reason_for_change && <p className="dim">Replanned because: {plan.reason_for_change}</p>}
            {plan ? <PlanGraph plan={plan} selected={sel} onSelect={setSel} /> : <p className="dim">Planning…</p>}
          </Card>
          {sel && (
            <Card>
              <StepDrilldown stepId={sel} calls={calls} obs={t.observations ?? []} />
            </Card>
          )}
          {t.kind === "change" && t.artifacts?.research && (
            <Card title="Research findings">
              <p>{t.artifacts.research.summary}</p>
              {t.artifacts.research.current_state && <p className="dim">{t.artifacts.research.current_state}</p>}
              {(t.artifacts.research.relevant_files ?? []).map((f, i) => (
                <p key={i}><b className="mono">{f.path}</b>: {f.why}</p>
              ))}
              {(t.artifacts.research.risks ?? []).map((r, i) => <p key={i} className="dim">Risk: {r}</p>)}
            </Card>
          )}
          {t.root_cause && (
            <Card title="Root cause">
              <p><b>{t.root_cause.file}</b> @ <span className="mono">{t.root_cause.commit_sha.slice(0, 10)}</span> (confidence {t.root_cause.confidence})</p>
              <p>{t.root_cause.summary}</p>
              {(t.artifacts?.rejected_root_causes ?? []).map((r, i) => (
                <p key={i} className="dim">Rejected earlier diagnosis: {r.file}: {r.summary}</p>
              ))}
            </Card>
          )}
          {lastV && (
            <Card title={`Verification (attempt ${lastV.attempt})`}>
              <Badge s={lastV.status} />
              <Gates gates={lastV.gates} />
              <Bench b={lastV.metrics.benchmark} />
              {lastV.issues.map((i, k) => <p key={k} className="err">{i.type}: {i.detail}</p>)}
            </Card>
          )}
          {t.artifacts?.patch && (
            <Card title="Patch">
              <DiffView patch={t.artifacts.patch} />
            </Card>
          )}
          {t.pull_request && (
            <Card title="Pull request">
              <p>
                {t.pull_request.url.startsWith("http") ? (
                  <a href={t.pull_request.url} target="_blank" rel="noreferrer">
                    #{t.pull_request.number} {t.pull_request.title} ↗
                  </a>
                ) : (
                  <>#{t.pull_request.number} {t.pull_request.title} <span className="dim">(local demo repo)</span></>
                )}
              </p>
            </Card>
          )}
          {mine.filter((a) => a.status !== "pending").map((a) => (
            <Card key={a.id} title="Decided approval">
              <Badge s={a.status} /> by {a.actor ?? "unknown"} <span className="dim">{a.tool}</span>
            </Card>
          ))}
        </div>
        <div>
          <Card title="Stats"><StatsPanel s={stats} /></Card>
          <Card><Timeline events={events} live={live} /></Card>
          {(t.human_feedback?.length ?? 0) > 0 && (
            <Card title="Human feedback">
              {t.human_feedback!.map((f, i) => <p key={i}><b>{f.actor}</b>: {f.feedback}</p>)}
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
