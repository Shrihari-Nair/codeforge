"use client";
import { AeosEvent } from "@/lib/api";

export default function Timeline({ events, live }: { events: AeosEvent[]; live: boolean }) {
  return (
    <div>
      <h2>
        Live events <span className={live ? "s-done badge" : "badge"}>{live ? "live" : "offline"}</span>
      </h2>
      <div className="tl mono">
        {events.map((e) => (
          <div key={e.id}>
            <span className="dim">{new Date(e.ts).toLocaleTimeString()}</span> <b>{e.agent}</b> {e.type}
            <span className="dim"> {e.message}</span>
          </div>
        ))}
        {!events.length && <span className="dim">No events yet.</span>}
      </div>
    </div>
  );
}
