"use client";
import { useEffect, useRef } from "react";
import { AeosEvent } from "@/lib/api";

const tone = (t: string) => (/fail|error|reject|escalat/.test(t) ? "err" : /complet|approv|passed|done/.test(t) ? "ok" : "");

export default function Timeline({ events, live }: { events: AeosEvent[]; live: boolean }) {
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => {
    box.current?.scrollTo({ top: box.current.scrollHeight, behavior: "smooth" });
  }, [events.length]);
  return (
    <div>
      <h2>
        Live events <span className={live ? "s-done badge" : "badge"}>{live ? "live" : "offline"}</span>
      </h2>
      <div className="tl mono" ref={box} aria-live="polite">
        {events.map((e) => (
          <div key={e.id}>
            <span className="dim">{new Date(e.ts).toLocaleTimeString()}</span> <b>{e.agent}</b> <span className={`ev ${tone(e.type)}`}>{e.type}</span>
            <span className="dim"> {e.message}</span>
          </div>
        ))}
        {!events.length && <span className="dim">Waiting for the first event.</span>}
      </div>
    </div>
  );
}
