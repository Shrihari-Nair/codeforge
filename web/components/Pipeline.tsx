"use client";
import type { TaskDetail } from "@/lib/api";
import { TERMINAL } from "@/lib/hooks";

const STAGES: [string, string | null][] = [
  ["Research", "research"],
  ["Analyze", "analytics"],
  ["Code", "coding"],
  ["Verify", "verifier"],
  ["Approve", null],
];
export type StageState = "pending" | "active" | "done" | "failed" | "retry" | "omit";

/** Derives each stage from the real plan: steps are grouped by owner agent. */
export function stageStates(t: TaskDetail | null): StageState[] {
  const open = !!t && !TERMINAL.includes(t.status); // a failed stage on a live task is being retried
  const plan = t?.plans?.find((p) => p.version === t.plan_version) ?? t?.plans?.[t.plans.length - 1];
  return STAGES.map(([, owner]) => {
    if (!t) return "pending";
    if (!owner) return t.status === "completed" ? "done" : t.status === "awaiting_approval" ? "active" : "pending";
    const steps = plan?.steps.filter((s) => s.owner === owner) ?? [];
    if (!steps.length) {
      const v = t.verifications?.[t.verifications.length - 1];
      if (owner === "verifier" && v) return v.status === "passed" ? "done" : open ? "retry" : "failed";
      return plan ? "omit" : "pending";
    }
    if (steps.some((s) => s.status === "failed")) return open ? "retry" : "failed";
    if (steps.some((s) => s.status === "running")) return "active";
    if (steps.every((s) => s.status === "done" || s.status === "skipped")) return "done";
    return "pending";
  });
}

export default function Pipeline({ task }: { task: TaskDetail | null }) {
  const st = stageStates(task);
  // A change request never runs the analytics agent, so that stage is left out instead of shown as skipped.
  const shown = STAGES.map(([label], i) => ({ label, state: st[i] })).filter((x) => x.state !== "omit");
  return (
    <ol className="pipe" aria-label="Pipeline progress">
      {shown.map(({ label, state }, i) => (
        <li key={label} className={`stage ${state}`} style={{ ["--i" as string]: i }}>
          <span className="dot" aria-hidden />
          <span className="lbl">{label}</span>
          <span className="sr">{state}</span>
        </li>
      ))}
    </ol>
  );
}
