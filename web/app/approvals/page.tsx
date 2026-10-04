"use client";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/hooks";
import ApprovalCard from "@/components/ApprovalCard";
import Link from "next/link";
import { ErrorNote } from "@/components/ui";

export default function Approvals() {
  const { data, error, refresh } = usePoll(() => api.approvals("pending"), 3000);
  return (
    <>
      <h1>Pending approvals</h1>
      <ErrorNote e={error} />
      {data && !data.length && <p className="dim">Nothing is waiting for you.</p>}
      {(data ?? []).map((a) => (
        <div key={a.id}>
          <Link href={`/tasks/${a.task_id}`} className="mono">task {a.task_id}</Link>
          <ApprovalCard a={a} onDone={refresh} />
        </div>
      ))}
    </>
  );
}
