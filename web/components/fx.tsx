"use client";
import { useEffect, useRef, useState } from "react";

const reduced = () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

/** Eases a number toward `to` (rAF, ~700ms). Tabular figures keep the width stable. */
export function CountUp({ to, dec = 0 }: { to: number; dec?: number }) {
  const [v, setV] = useState(0);
  const cur = useRef(0);
  useEffect(() => {
    if (reduced()) {
      cur.current = to;
      return setV(to);
    }
    let raf = 0;
    const from = cur.current;
    const t0 = performance.now();
    const step = (t: number) => {
      const p = Math.min(1, (t - t0) / 700);
      cur.current = from + (to - from) * (1 - Math.pow(1 - p, 3));
      setV(cur.current);
      if (p < 1) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [to]);
  return <>{v.toFixed(dec)}</>;
}

const GLYPHS = "01<>/{}#$%&";
/** Decode-in text reveal on mount. Final text is always in the DOM for screen readers. */
export function Scramble({ text }: { text: string }) {
  const [out, setOut] = useState(text);
  useEffect(() => {
    if (reduced()) return;
    let f = 0;
    const id = setInterval(() => {
      f++;
      setOut(
        text
          .split("")
          .map((c, i) => (c === " " || c === "\n" || i < f * 1.6 ? c : GLYPHS[Math.floor(Math.random() * GLYPHS.length)]))
          .join(""),
      );
      if (f * 1.6 >= text.length) clearInterval(id);
    }, 28);
    return () => clearInterval(id);
  }, [text]);
  return (
    <>
      <span aria-hidden>{out}</span>
      <span style={{ position: "absolute", clip: "rect(0 0 0 0)" }}>{text}</span>
    </>
  );
}
