"use client";
import { Observation, ToolCall } from "@/lib/api";
import { Badge, Json } from "./ui";

export default function StepDrilldown({ stepId, calls, obs }: { stepId: string; calls: ToolCall[]; obs: Observation[] }) {
  const c = calls.filter((x) => x.step_id === stepId);
  const o = obs.filter((x) => x.step_id === stepId);
  return (
    <div>
      <h2>Step {stepId}</h2>
      {o.map((x) => (
        <div key={x.id} style={{ marginBottom: 10 }}>
          <div className="row">
            <b>{x.agent}</b>
            <Badge s={x.origin === "untrusted" ? "failed" : "done"} />
            <span className="dim">{x.origin}</span>
          </div>
          <pre>{x.summary}</pre>
        </div>
      ))}
      <h2>Tool calls ({c.length})</h2>
      {c.map((t) => (
        <details key={t.id} style={{ marginBottom: 6 }}>
          <summary>
            <span className={t.is_error ? "err" : ""}>
              {t.server}.{t.tool}
            </span>{" "}
            <span className="dim">
              {t.risk} · {Math.round(t.duration_ms)} ms
            </span>
          </summary>
          <Json v={t.arguments} />
          <pre className="dim">{t.result_preview}</pre>
        </details>
      ))}
      {!c.length && !o.length && <p className="dim">Nothing recorded for this step yet.</p>}
    </div>
  );
}
