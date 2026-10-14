"use client";
import { useRef, useState } from "react";
import type { EvalAggregate, EvalDelta } from "@/lib/api";

export const pct = (x: number) => `${Math.round(x * 100)}%`;
export const pretty = (s: string) => s.replace(/_/g, " ");
const money = (x: number) => `$${x.toFixed(x < 0.1 ? 4 : 2)}`;

/** Formats a metric value the way its name implies (rates as %, cost as $, the rest as numbers). */
export function fmtMetric(metric: string, v: number) {
  if (metric === "avg_cost_usd") return money(v);
  if (metric === "avg_latency_s") return `${v.toFixed(1)} s`;
  if (metric.startsWith("avg_")) return v.toFixed(1);
  return pct(v);
}

/** Horizontal bars: success rate per template. One series, so no legend; the value sits at the tip. */
export function TemplateBars({ data }: { data: Record<string, EvalAggregate> }) {
  const rows = Object.entries(data).sort((a, b) => b[1].success_rate.mean - a[1].success_rate.mean);
  const [hover, setHover] = useState<string | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const tip = useRef<HTMLDivElement>(null);
  const place = (e: React.PointerEvent | React.FocusEvent, key: string) => {
    setHover(key);
    const b = box.current?.getBoundingClientRect();
    if (!b || !tip.current) return;
    const x = "clientX" in e ? e.clientX : b.left + b.width / 2;
    const y = "clientY" in e ? e.clientY : b.top + 40;
    tip.current.style.transform = `translate(${Math.min(x - b.left + 16, b.width - 260)}px, ${y - b.top + 12}px)`;
  };
  const h = hover ? data[hover] : null;
  return (
    <div className="bars" ref={box} onPointerLeave={() => setHover(null)}>
      <div className="bgrid" aria-hidden>
        {[0, 25, 50, 75, 100].map((t) => (
          <span key={t} style={{ left: `${t}%` }}><i>{t}%</i></span>
        ))}
      </div>
      {rows.map(([name, a]) => (
        <div
          key={name}
          className={`brow ${hover === name ? "on" : ""}`}
          tabIndex={0}
          role="img"
          aria-label={`${pretty(name)}: ${pct(a.success_rate.mean)} success over ${a.tasks} tasks`}
          onPointerMove={(e) => place(e, name)}
          onFocus={(e) => place(e, name)}
          onBlur={() => setHover(null)}
        >
          <span className="blab">{pretty(name)}</span>
          <span className="bplot">
            {a.success_rate.mean > 0 && <span className="ebar" style={{ width: `${a.success_rate.mean * 100}%` }} />}
            <span className="bval" style={{ left: `calc(${a.success_rate.mean * 100}% + 10px)` }}>
              {pct(a.success_rate.mean)}
            </span>
          </span>
        </div>
      ))}
      <div className="tip" ref={tip} style={{ opacity: h ? 1 : 0 }} role="status">
        {hover && h && (
          <>
            <b>{pretty(hover)}</b>
            <span>{h.tasks} tasks</span>
            <dl>
              <dt>Success</dt><dd>{pct(h.success_rate.mean)}</dd>
              <dt>Root cause</dt><dd>{pct(h.root_cause_accuracy.mean)}</dd>
              <dt>Recovery</dt><dd>{h.recovery_rate.n ? pct(h.recovery_rate.mean) : "n/a"}</dd>
              <dt>Cost</dt><dd>{money(h.avg_cost_usd.mean)}</dd>
              <dt>Latency</dt><dd>{h.avg_latency_s.mean.toFixed(0)} s</dd>
            </dl>
          </>
        )}
      </div>
    </div>
  );
}

/** Same numbers as the chart, as a table: nothing is reachable only through hover. */
export function TemplateTable({ data }: { data: Record<string, EvalAggregate> }) {
  const rows = Object.entries(data).sort((a, b) => b[1].success_rate.mean - a[1].success_rate.mean);
  return (
    <div className="tbl">
      <table>
        <thead>
          <tr><th>Template</th><th>Tasks</th><th>Success</th><th>Root cause</th><th>Recovery</th><th>Cost</th><th>Latency</th></tr>
        </thead>
        <tbody>
          {rows.map(([name, a]) => (
            <tr key={name}>
              <td>{pretty(name)}</td>
              <td>{a.tasks}</td>
              <td>{pct(a.success_rate.mean)}</td>
              <td>{pct(a.root_cause_accuracy.mean)}</td>
              <td>{a.recovery_rate.n ? pct(a.recovery_rate.mean) : "n/a"}</td>
              <td>{money(a.avg_cost_usd.mean)}</td>
              <td>{a.avg_latency_s.mean.toFixed(0)} s</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Before/after per metric. Status is icon + word, never colour alone. */
export function Deltas({ deltas }: { deltas: EvalDelta[] }) {
  const rows = [...deltas].sort((a, b) => Number(b.regressed) - Number(a.regressed));
  // Cheaper/faster is not "better" when the run failed its tasks: it usually just stopped early.
  const qualityDropped = deltas.some((d) => d.metric === "success_rate" && d.regressed);
  return (
    <ul className="deltas">
      {rows.map((d) => {
        const same = Math.abs(d.delta) < 1e-9;
        const effort = d.metric.startsWith("avg_");
        const state = d.regressed
          ? "regressed"
          : same
            ? "unchanged"
            : effort && qualityDropped
              ? d.delta < 0 ? "lower" : "higher"
              : "improved";
        return (
          <li key={d.metric}>
            <span className="g">{d.label}</span>
            <span className="mono dim">{fmtMetric(d.metric, d.previous)} → {fmtMetric(d.metric, d.current)}</span>
            <span className={`chg ${state}`}>
              <i aria-hidden>{state === "regressed" || state === "lower" ? "▼" : state === "improved" || state === "higher" ? "▲" : "="}</i>
              {state}
            </span>
          </li>
        );
      })}
    </ul>
  );
}
