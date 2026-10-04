"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { AeosEvent, api, eventStreamUrl } from "./api";

/** Poll `fn` every `ms`; `stop(data)` ends polling (e.g. task finished). State lives in the backend. */
export function usePoll<T>(fn: () => Promise<T>, ms: number, deps: unknown[] = [], stop?: (d: T) => boolean) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const stopped = useRef(false);
  const refresh = useCallback(async () => {
    try {
      const d = await fn();
      setData(d);
      setError(null);
      if (stop?.(d)) stopped.current = true;
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, deps); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    stopped.current = false;
    refresh();
    const t = setInterval(() => {
      if (!stopped.current) refresh();
    }, ms);
    return () => clearInterval(t);
  }, [refresh, ms]);
  return { data, error, refresh };
}

/** Full event history from the backend, then live via SSE. Reconnects resume from the last id. */
export function useEvents(taskId: string) {
  const [events, setEvents] = useState<AeosEvent[]>([]);
  const [live, setLive] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let es: EventSource | null = null;
    let last = 0;
    const seen = new Set<number>();
    const add = (batch: AeosEvent[]) => {
      const fresh = batch.filter((e) => !seen.has(e.id));
      if (!fresh.length) return;
      fresh.forEach((e) => seen.add(e.id));
      last = Math.max(last, ...fresh.map((e) => e.id));
      setEvents((prev) => [...prev, ...fresh].sort((a, b) => a.id - b.id));
    };
    (async () => {
      try {
        add(await api.events(taskId, 0));
      } catch {}
      if (cancelled) return;
      es = new EventSource(eventStreamUrl(taskId, last));
      es.onopen = () => setLive(true);
      es.onerror = () => setLive(false); // the browser reconnects and re-sends Last-Event-ID
      es.addEventListener("aeos", (m) => add([JSON.parse((m as MessageEvent).data)]));
    })();
    return () => {
      cancelled = true;
      es?.close();
    };
  }, [taskId]);
  return { events, live };
}

export function useActor(): [string, (v: string) => void] {
  const [actor, setActor] = useState("");
  useEffect(() => setActor(localStorage.getItem("aeos.actor") ?? "alice"), []);
  const set = (v: string) => {
    setActor(v);
    localStorage.setItem("aeos.actor", v);
  };
  return [actor, set];
}

export const TERMINAL = ["completed", "failed", "escalated", "cancelled"];
