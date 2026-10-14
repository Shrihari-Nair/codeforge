"use client";
import { useCallback, useEffect, useMemo, useState, type ReactNode } from "react";
import { usePathname, useRouter } from "next/navigation";
import { api } from "@/lib/api";
import { AuthCtx } from "@/lib/auth";
import Nav from "./Nav";
import Palette from "./Palette";

/** Asks the API whether login is on. If it is and nobody is signed in, go to /login.
 *  If login is off (the default) or the API is unreachable, the app renders as usual. */
export default function AuthGate({ children }: { children: ReactNode }) {
  const [state, setState] = useState({ ready: false, authRequired: false, user: null as string | null });
  const path = usePathname();
  const router = useRouter();
  const onLogin = path === "/login";

  const refresh = useCallback(async () => {
    try {
      const r = await api.me();
      setState({ ready: true, authRequired: r.auth_required, user: r.user });
    } catch {
      setState((s) => ({ ...s, ready: true }));
    }
  }, []);
  useEffect(() => {
    refresh();
  }, [refresh]);
  useEffect(() => {
    if (state.ready && state.authRequired && !state.user && !onLogin) router.replace("/login");
  }, [state, onLogin, router]);

  const value = useMemo(() => ({ ...state, refresh }), [state, refresh]);
  if (!state.ready || (state.authRequired && !state.user && !onLogin)) return null;
  return (
    <AuthCtx.Provider value={value}>
      {!onLogin && (
        <>
          <Nav />
          <Palette />
        </>
      )}
      <main id="main">{children}</main>
    </AuthCtx.Provider>
  );
}
