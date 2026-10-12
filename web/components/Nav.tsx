"use client";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";

export default function Nav() {
  const path = usePathname();
  const { data: meta, error } = usePoll(() => api.meta(), 15000);
  const { data: pending } = usePoll(() => api.approvals("pending"), 5000);
  const n = pending?.length ?? 0;
  const link = (href: string, label: string, on: boolean) => (
    <Link href={href} className={on ? "on" : ""}>
      {label}
      {href === "/approvals" && n > 0 && <span className="cnt">{n}</span>}
    </Link>
  );
  return (
    <header className="top">
      <Link href="/" className="brand">
        <span className="mark">A</span>AEOS
      </Link>
      <nav className="navlinks" aria-label="Main">
        {link("/", "Tasks", path === "/" || path.startsWith("/tasks"))}
        {link("/approvals", "Approvals", path.startsWith("/approvals"))}
      </nav>
      <span className="sp" />
      <button className="kbtn" onClick={() => window.dispatchEvent(new Event("aeos:palette"))} aria-label="Open command palette">
        Search <kbd>Ctrl K</kbd>
      </button>
      {meta && <span className="pill hide mono">{meta.llm_provider}</span>}
      <span className={`pill ${error ? "warn" : meta ? "live" : ""}`}>
        <i />
        {error ? "API offline" : meta ? "API live" : "Connecting"}
      </span>
    </header>
  );
}
