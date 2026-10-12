"use client";
import { Stats } from "@/lib/api";
import { CountUp } from "./fx";

export default function StatsPanel({ s }: { s: Stats | null }) {
  if (!s) return <p className="dim">Stats appear once the task starts.</p>;
  const tiles: [string, number, number, string][] = [
    ["Duration", s.duration_s ?? 0, 1, " s"],
    ["Cost", s.llm.cost_usd, 4, ""],
    ["Tokens", s.llm.tokens, 0, ""],
    ["Tool calls", s.tools.calls, 0, ""],
  ];
  const rows: [string, string][] = [
    ["LLM calls", String(s.llm.calls)],
    ["Tool errors", String(s.tools.errors)],
    ["Replans / retries", `${s.replans} / ${s.step_retries}`],
    ["Verifications", `${s.verifications.total} (${s.verifications.failed} failed)`],
    ["Approval wait", `${s.approvals.total_wait_s.toFixed(0)} s`],
    ["Memory recalled", String(s.memory.retrieved)],
  ];
  return (
    <>
      <div className="mini">
        {tiles.map(([k, v, d, u]) => (
          <div key={k}>
            <b>{k === "Cost" && "$"}<CountUp to={v} dec={d} />{u}</b>
            <span>{k}</span>
          </div>
        ))}
      </div>
      <div className="kv">
        {rows.map(([k, v]) => (
          <span key={k} style={{ display: "contents" }}>
            <span>{k}</span>
            <span>{v}</span>
          </span>
        ))}
      </div>
    </>
  );
}
