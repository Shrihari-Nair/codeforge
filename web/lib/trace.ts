import type { AeosEvent, ToolCall } from "./api";

export interface Call {
  t: number; // seconds from the start of the run
  dur: number; // seconds
  name: string;
  err: boolean;
}
export interface Lane {
  id: string;
  label: string;
  start: number;
  end: number;
  state: "done" | "failed" | "running";
  calls: Call[];
}
export interface Moment {
  t: number;
  text: string;
  tone: "ok" | "bad" | "info" | "wait";
}
export interface Trace {
  total: number;
  lanes: Lane[];
  moments: Moment[];
  wait: { start: number; end: number } | null; // the human-approval pause
}

const sec = (iso: string) => Date.parse(iso) / 1000;

/** Builds the waterfall from what the backend already records: events (step start/finish,
 *  verification, replan, approval) and tool calls (exact start and duration). */
export function buildTrace(events: AeosEvent[], calls: ToolCall[]): Trace | null {
  if (!events.length) return null;
  const t0 = sec(events[0].ts);
  const last = events[events.length - 1];
  const total = Math.max(sec(last.ts) - t0, 0.001);
  const rel = (iso: string) => Math.max(sec(iso) - t0, 0);

  const lanes = new Map<string, Lane>();
  const lane = (id: string, agent: string): Lane => {
    let l = lanes.get(id);
    if (!l) lanes.set(id, (l = { id, label: `${id} ${agent}`, start: -1, end: -1, state: "running", calls: [] }));
    return l;
  };
  const moments: Moment[] = [];
  let waitStart: number | null = null;
  let wait: Trace["wait"] = null;

  for (const e of events) {
    const t = rel(e.ts);
    if (e.type === "step_started") {
      const m = /^(\S+) → (\w+)/.exec(e.message);
      if (m) lane(m[1], m[2]).start = t;
    } else if (e.type === "step_done" || e.type === "step_failed") {
      const m = /^(\S+) \((\w+)\)/.exec(e.message);
      if (m) {
        const l = lane(m[1], m[2]);
        l.end = t;
        l.state = e.type === "step_done" ? "done" : "failed";
      }
    } else if (e.type === "plan_created") moments.push({ t, text: e.message.split(":")[0].replace(/^plan/, "Plan"), tone: "info" });
    else if (e.type === "verification") {
      const ok = !e.message.includes("failed");
      moments.push({ t, text: ok ? "Verification passed" : `Verification failed${/: (\w+)/.exec(e.message)?.[1] ? ` (${/: (\w+)/.exec(e.message)![1]})` : ""}`, tone: ok ? "ok" : "bad" });
    } else if (e.type === "replan") moments.push({ t, text: e.message.split(" (")[0].replace(/^plan/, "Replanned: plan"), tone: "info" });
    else if (e.type === "approval_requested") {
      waitStart = t;
      moments.push({ t, text: "Waiting for a human to approve", tone: "wait" });
    } else if (e.type === "approval_decided") {
      if (waitStart != null) wait = { start: waitStart, end: t };
      moments.push({ t, text: e.message.replace(/^approval /, "").replace(/^(\w)/, (c) => c.toUpperCase()), tone: e.message.includes("rejected") ? "bad" : "ok" });
    } else if (e.type === "pr_created") moments.push({ t, text: e.message.split(" (")[0].replace(/^pull request/, "Pull request"), tone: "ok" });
    else if (e.type === "task_completed") moments.push({ t, text: "Task completed", tone: "ok" });
  }

  for (const c of calls) {
    const l = lanes.get(c.step_id) ?? lane(c.step_id, c.agent);
    l.calls.push({ t: Math.max(c.ts - t0, 0), dur: c.duration_ms / 1000, name: `${c.server}.${c.tool}`, err: c.is_error });
  }
  for (const l of lanes.values()) {
    // No start/finish event for this step: draw it from its own tool calls. A step that started
    // but has not finished is still running, so it extends to the latest event.
    const started = l.start >= 0;
    const ends = l.calls.map((c) => c.t + c.dur);
    if (!started) l.start = l.calls.length ? Math.min(...l.calls.map((c) => c.t)) : 0;
    if (l.end < 0) l.end = started || !ends.length ? total : Math.max(l.start, ...ends);
  }
  const ordered = [...lanes.values()].sort((a, b) => a.start - b.start);
  return { total, lanes: ordered, moments, wait };
}
