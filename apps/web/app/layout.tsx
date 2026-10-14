import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Coop-Planung",
  description: "Lokales M1-Dashboard für Kooperationen",
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
              Coop Planung <small>M1 · lokal</small>
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
