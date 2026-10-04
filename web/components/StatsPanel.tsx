"use client";
import { Stats } from "@/lib/api";

export default function StatsPanel({ s }: { s: Stats | null }) {
  if (!s) return <p className="dim">Stats appear once the task starts.</p>;
  const rows: [string, string][] = [
    ["Duration", s.duration_s != null ? `${s.duration_s.toFixed(1)} s` : "—"],
    ["LLM calls", `${s.llm.calls} (${s.llm.tokens.toLocaleString()} tokens)`],
    ["Cost", `$${s.llm.cost_usd.toFixed(4)}`],
    ["Tool calls", `${s.tools.calls} (${s.tools.errors} errors)`],
    ["Replans / retries", `${s.replans} / ${s.step_retries}`],
    ["Verifications", `${s.verifications.total} (${s.verifications.failed} failed)`],
    ["Approval wait", `${s.approvals.total_wait_s.toFixed(0)} s`],
    ["Memory recalled", String(s.memory.retrieved)],
  ];
  return (
    <div className="kv">
      {rows.map(([k, v]) => (
        <span key={k} style={{ display: "contents" }}>
          <span className="dim">{k}</span>
          <span>{v}</span>
        </span>
      ))}
    </div>
  );
}
