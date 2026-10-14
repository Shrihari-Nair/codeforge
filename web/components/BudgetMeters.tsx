"use client";
import type { TaskDetail } from "@/lib/api";

type B = NonNullable<TaskDetail["budget"]> & Record<string, number | null>;

/** Spend against each cap. The fill turns amber then red as a cap gets close; the track is a
 *  lighter step of the same ramp so the whole bar reads. */
export default function BudgetMeters({ b }: { b?: TaskDetail["budget"] }) {
  if (!b) return null;
  const x = b as B;
  const wall = x.started_at ? ((x.finished_at ?? Date.now() / 1000) - x.started_at) : 0;
  const rows: [string, number, number, string][] = [
    ["Cost", x.cost_usd, x.max_cost_usd as number, `$${x.cost_usd.toFixed(4)} of $${(x.max_cost_usd as number).toFixed(2)}`],
    ["Tokens", x.tokens_used, x.max_tokens as number, `${x.tokens_used.toLocaleString()} of ${(x.max_tokens as number).toLocaleString()}`],
    ["Agent steps", x.steps_used as number, x.max_steps as number, `${x.steps_used} of ${x.max_steps}`],
    ["Time", wall, x.max_wall_seconds as number, `${wall.toFixed(0)} s of ${(x.max_wall_seconds as number).toFixed(0)} s`],
  ];
  return (
    <div className="meters">
      <h2>Budget used</h2>
      {rows.map(([k, v, max, label]) => {
        const f = max > 0 ? Math.min(v / max, 1) : 0;
        const sev = f > 0.9 ? "hot" : f > 0.75 ? "warm" : "";
        return (
          <div key={k} className="meter">
            <span>{k}</span>
            <span className="mono dim">{label}</span>
            <span className={`mtrack ${sev}`} role="meter" aria-label={k} aria-valuenow={Math.round(f * 100)} aria-valuemin={0} aria-valuemax={100}>
              <i style={{ width: `${Math.max(f * 100, v > 0 ? 1.5 : 0)}%` }} />
            </span>
          </div>
        );
      })}
    </div>
  );
}
