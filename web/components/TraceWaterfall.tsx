"use client";
import { useMemo, useRef, useState } from "react";
import type { AeosEvent, ToolCall } from "@/lib/api";
import { buildTrace } from "@/lib/trace";

const s = (x: number) => (x >= 10 ? `${x.toFixed(0)} s` : x >= 1 ? `${x.toFixed(1)} s` : `${Math.round(x * 1000)} ms`);

export default function TraceWaterfall({ events, calls }: { events: AeosEvent[]; calls: ToolCall[] }) {
  const tr = useMemo(() => buildTrace(events, calls), [events, calls]);
  const [tip, setTip] = useState<{ title: string; lines: string[] } | null>(null);
  const box = useRef<HTMLDivElement>(null);
  const tipEl = useRef<HTMLDivElement>(null);
  if (!tr) return <p className="dim">The trace appears once the task starts.</p>;
  const x = (t: number) => `${(t / tr.total) * 100}%`;
  const show = (e: React.PointerEvent | React.FocusEvent, title: string, lines: string[]) => {
    setTip({ title, lines });
    const b = box.current?.getBoundingClientRect();
    if (!b || !tipEl.current) return;
    const px = "clientX" in e ? e.clientX : b.left + 120;
    const py = "clientY" in e ? e.clientY : b.top + 40;
    tipEl.current.style.transform = `translate(${Math.min(px - b.left + 14, b.width - 250)}px, ${py - b.top + 14}px)`;
  };
  const ticks = [0, 0.25, 0.5, 0.75, 1].map((f) => f * tr.total);
  return (
    <div>
      <div className="wf" ref={box} onPointerLeave={() => setTip(null)}>
        <div className="wf-axis" aria-hidden>
          {ticks.map((t) => (
            <span key={t} style={{ left: x(t) }}><i>{s(t)}</i></span>
          ))}
        </div>
        {tr.lanes.map((l) => (
          <div key={l.id} className="wf-row">
            <span className="wf-lab">{l.label}</span>
            <span className="wf-plot">
              <span
                className={`wf-bar ${l.state}`}
                style={{ left: x(l.start), width: `max(${((l.end - l.start) / tr.total) * 100}%, 6px)` }}
                tabIndex={0}
                aria-label={`${l.label}: ${l.state}, ${s(l.end - l.start)}, ${l.calls.length} tool calls`}
                onPointerMove={(e) => show(e, l.label, [`${l.state}, ${s(l.end - l.start)}`, `${l.calls.length} tool calls`])}
                onFocus={(e) => show(e, l.label, [`${l.state}, ${s(l.end - l.start)}`, `${l.calls.length} tool calls`])}
                onBlur={() => setTip(null)}
              />
              {l.calls.map((c, i) => (
                <span
                  key={i}
                  className={`wf-call ${c.err ? "err" : ""}`}
                  style={{ left: x(c.t), width: `max(${(c.dur / tr.total) * 100}%, 3px)` }}
                  onPointerMove={(e) => show(e, c.name, [`took ${s(c.dur)}`, c.err ? "returned an error" : "ok"])}
                />
              ))}
            </span>
          </div>
        ))}
        {tr.wait && (
          <div className="wf-row">
            <span className="wf-lab">human approval</span>
            <span className="wf-plot">
              <span
                className="wf-bar wait"
                style={{ left: x(tr.wait.start), width: `max(${((tr.wait.end - tr.wait.start) / tr.total) * 100}%, 6px)` }}
                tabIndex={0}
                aria-label={`Waited ${s(tr.wait.end - tr.wait.start)} for a human`}
                onPointerMove={(e) => show(e, "Waiting for a human", [s(tr.wait!.end - tr.wait!.start)])}
                onFocus={(e) => show(e, "Waiting for a human", [s(tr.wait!.end - tr.wait!.start)])}
                onBlur={() => setTip(null)}
              />
            </span>
          </div>
        )}
        <div className="tip" ref={tipEl} style={{ opacity: tip ? 1 : 0 }} role="status">
          {tip && (
            <>
              <b>{tip.title}</b>
              {tip.lines.map((l) => <span key={l} style={{ display: "block" }}>{l}</span>)}
            </>
          )}
        </div>
      </div>
      <ul className="wf-key" aria-label="Legend">
        <li><i className="wf-sw done" />step</li>
        <li><i className="wf-sw call" />tool call</li>
        <li><i className="wf-sw failed" />failed step</li>
        <li><i className="wf-sw wait" />human wait</li>
      </ul>
      <ol className="moments">
        {tr.moments.map((m, i) => (
          <li key={i} className={m.tone}>
            <span className="mono dim">{s(m.t)}</span>
            <i aria-hidden>{m.tone === "ok" ? "✓" : m.tone === "bad" ? "✗" : m.tone === "wait" ? "…" : "·"}</i>
            {m.text}
          </li>
        ))}
      </ol>
    </div>
  );
}
