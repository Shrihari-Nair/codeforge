"use client";
import { PlanStep, PlanVersion } from "@/lib/api";

function levels(steps: PlanStep[]): PlanStep[][] {
  const depth = new Map<string, number>();
  const byId = new Map(steps.map((s) => [s.id, s]));
  const d = (s: PlanStep, seen = new Set<string>()): number => {
    if (depth.has(s.id)) return depth.get(s.id)!;
    if (seen.has(s.id)) return 0;
    seen.add(s.id);
    const v = s.depends_on.length ? 1 + Math.max(...s.depends_on.map((x) => (byId.get(x) ? d(byId.get(x)!, seen) : 0))) : 0;
    depth.set(s.id, v);
    return v;
  };
  steps.forEach((s) => d(s));
  const out: PlanStep[][] = [];
  steps.forEach((s) => (out[depth.get(s.id)!] ??= []).push(s));
  return out.filter(Boolean);
}

export default function PlanGraph({ plan, selected, onSelect }: { plan: PlanVersion; selected: string | null; onSelect: (id: string) => void }) {
  return (
    <div className="plan">
      {levels(plan.steps).map((col, i) => (
        <div className="col" key={i}>
          {col.map((s) => (
            <div key={s.id} className={`node n-${s.status} ${selected === s.id ? "sel" : ""}`} onClick={() => onSelect(s.id)}>
              <div className="own">
                {s.owner} · {s.status}
                {s.attempts > 1 ? ` · attempt ${s.attempts}` : ""}
              </div>
              <div>{s.description}</div>
              {s.depends_on.length > 0 && <div className="dim mono">after {s.depends_on.join(", ")}</div>}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}
