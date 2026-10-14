"use client";
import { createContext, useContext } from "react";

export interface Me {
  ready: boolean; // the first /auth/me answer has arrived
  authRequired: boolean;
  user: string | null;
  refresh: () => Promise<void>;
}

export const AuthCtx = createContext<Me>({ ready: false, authRequired: false, user: null, refresh: async () => {} });
export const useMe = () => useContext(AuthCtx);
