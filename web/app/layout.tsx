import "./globals.css";
import { Inter, Geist_Mono } from "next/font/google";
import type { ReactNode } from "react";
import Nav from "@/components/Nav";
import Palette from "@/components/Palette";

const inter = Inter({ subsets: ["latin"], variable: "--f-sans", display: "swap" });
const mono = Geist_Mono({ subsets: ["latin"], variable: "--f-mono", display: "swap" });

export const metadata = { title: "AEOS", description: "Autonomous Engineering OS dashboard" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en" className={`${inter.variable} ${mono.variable}`}>
      <body>
        <a className="skip" href="#main">Skip to content</a>
        <Nav />
        <Palette />
        <main id="main">{children}</main>
      </body>
    </html>
  );
}
