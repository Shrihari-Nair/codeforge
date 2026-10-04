import "./globals.css";
import Link from "next/link";
import type { ReactNode } from "react";

export const metadata = { title: "AEOS", description: "Autonomous Engineering OS dashboard" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        <header className="top">
          <b>AEOS</b>
          <Link href="/">Tasks</Link>
          <Link href="/approvals">Approvals</Link>
        </header>
        <main>{children}</main>
      </body>
    </html>
  );
}
