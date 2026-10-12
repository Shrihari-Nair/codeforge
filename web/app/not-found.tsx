import Link from "next/link";

export default function NotFound() {
  return (
    <section className="empty" style={{ paddingTop: 96 }}>
      <h1 className="display" style={{ minHeight: 0 }}>404</h1>
      <p className="lead" style={{ margin: "16px auto" }}>That page doesn't exist. The task may have been removed, or the link is wrong.</p>
      <Link href="/">Back to tasks</Link>
    </section>
  );
}
