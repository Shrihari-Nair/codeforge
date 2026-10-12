"use client";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";

type Item = { id: string; label: string; hint: string; go: () => void };

export default function Palette() {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [dyn, setDyn] = useState<Item[]>([]);
  const input = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const key = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") setOpen(false);
    };
    const show = () => setOpen(true);
    window.addEventListener("keydown", key);
    window.addEventListener("aeos:palette", show);
    return () => {
      window.removeEventListener("keydown", key);
      window.removeEventListener("aeos:palette", show);
    };
  }, []);

  useEffect(() => {
    if (!open) return;
    setQ("");
    setSel(0);
    input.current?.focus();
    const go = (href: string) => () => router.push(href);
    Promise.all([api.tasks().catch(() => []), api.approvals("pending").catch(() => [])]).then(([tasks, apps]) =>
      setDyn([
        ...apps.map((a) => ({ id: `a${a.id}`, label: `Review: ${a.arguments.title ?? a.tool}`, hint: "approval", go: go(`/tasks/${a.task_id}`) })),
        ...tasks.slice(0, 8).map((t) => ({ id: t.task_id, label: t.goal.slice(0, 70), hint: t.status.replace("_", " "), go: go(`/tasks/${t.task_id}`) })),
      ]),
    );
  }, [open, router]);

  const items = useMemo<Item[]>(() => {
    const base: Item[] = [
      { id: "new", label: "New task", hint: "action", go: () => { router.push("/"); setTimeout(() => document.getElementById("composer-goal")?.focus(), 150); } },
      { id: "tasks", label: "Go to tasks", hint: "page", go: () => router.push("/") },
      { id: "approvals", label: "Go to approvals", hint: "page", go: () => router.push("/approvals") },
    ];
    const all = [...base, ...dyn];
    const s = q.trim().toLowerCase();
    return s ? all.filter((i) => i.label.toLowerCase().includes(s) || i.hint.includes(s)) : all;
  }, [q, dyn, router]);

  if (!open) return null;
  const run = (i?: Item) => {
    if (!i) return;
    setOpen(false);
    i.go();
  };
  return (
    <div className="scrim" onMouseDown={() => setOpen(false)}>
      <div className="palette" role="dialog" aria-modal="true" aria-label="Command palette" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={input}
          value={q}
          placeholder="Search tasks, approvals, actions"
          aria-label="Search"
          onChange={(e) => { setQ(e.target.value); setSel(0); }}
          onKeyDown={(e) => {
            if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, items.length - 1)); }
            if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
            if (e.key === "Enter") run(items[sel]);
          }}
        />
        <ul role="listbox">
          {items.map((it, i) => (
            <li key={it.id} role="option" aria-selected={i === sel} className={i === sel ? "on" : ""} onMouseEnter={() => setSel(i)} onClick={() => run(it)}>
              <span className="g">{it.label}</span>
              <span className="dim">{it.hint}</span>
            </li>
          ))}
          {!items.length && <li className="empty">Nothing matches "{q}".</li>}
        </ul>
      </div>
    </div>
  );
}
