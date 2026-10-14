import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

const demo = process.env.NEXT_PUBLIC_DEMO_MODE === "true";

export const metadata: Metadata = demo
  ? {
      title: "Coop-Planung · öffentliche Demo",
      description:
        "Sicherer statischer Click-Dummy mit synthetischen Beispieldaten",
    }
  : {
      title: "Coop-Planung",
      description:
        "Arbeitsoberfläche für die Planung von Kooperationen und Veröffentlichungen",
    };

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="de">
      <body>
        <div className="app-shell">
          <aside className="sidebar">
            <a className="skip-link" href="#main">
              Zum Inhalt springen
            </a>
            <div className="wordmark">
              Coop Planung <small>{demo ? "Öffentliche Demo" : "M2"}</small>
            </div>
            <nav aria-label="Hauptnavigation">
              <Link href="/coops">Kooperationen</Link>
              <Link href="/planung">Planung</Link>
            </nav>
          </aside>
          <header className="mobile-topbar">
            <span>Coop Planung</span>
            <nav aria-label="Mobile Hauptnavigation">
              <Link href="/coops">Kooperationen</Link>
              <Link href="/planung">Planung</Link>
            </nav>
          </header>
          <main id="main">{children}</main>
        </div>
      </body>
    </html>
  );
}
