"use client";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { useMe } from "@/lib/auth";

export default function Login() {
  const router = useRouter();
  const { refresh } = useMe();
  const [name, setName] = useState("");
  const [pw, setPw] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setErr(null);
    try {
      await api.login(name.trim(), pw);
      await refresh();
      router.replace("/");
    } catch (x) {
      setErr(x instanceof Error ? x.message : String(x));
      setBusy(false);
    }
  };

  return (
    <section className="login">
      <span className="mark big">A</span>
      <h1>Sign in to AEOS</h1>
      <p className="dim">Approvals are recorded under the name you sign in with.</p>
      <form className="panel" onSubmit={submit}>
        <label>
          <span>Username</span>
          <input autoFocus autoComplete="username" value={name} onChange={(e) => setName(e.target.value)} required />
        </label>
        <label>
          <span>Password</span>
          <input type="password" autoComplete="current-password" value={pw} onChange={(e) => setPw(e.target.value)} required />
        </label>
        {err && <p className="err" role="alert">{err}</p>}
        <button className="primary" disabled={busy || !name.trim() || !pw}>{busy ? "Signing in" : "Sign in"}</button>
      </form>
    </section>
  );
}
