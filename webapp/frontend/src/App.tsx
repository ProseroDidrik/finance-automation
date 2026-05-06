import { useEffect, useState } from "react";
import { Sun, Moon } from "lucide-react";
import { CoverageReport } from "./components/CoverageReport";
import { PersonnelReport } from "./components/PersonnelReport";
import { PnlPivot } from "./components/PnlPivot";
import { Counterparties } from "./components/Counterparties";
import { Suppliers } from "./components/Suppliers";

type Tab = "pnl" | "coverage" | "personnel" | "counterparties" | "suppliers";

export default function App() {
  const [tab, setTab] = useState<Tab>("pnl");
  const [theme, setTheme] = useState<"dark" | "light">("light");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  return (
    <div className="min-h-screen bg-bg text-fg">
      {/* Header */}
      <header className="sticky top-0 z-10 bg-bg/95 backdrop-blur border-b border-border">
        <div className="max-w-screen-2xl mx-auto px-6 py-3 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <img
              src={theme === "dark" ? "/logo-white.png" : "/logo-black.png"}
              alt="Prosero Security Group"
              className="h-6 w-auto"
            />
            <span className="text-fg-muted text-sm border-l border-border pl-3">
              {tab === "pnl" ? "Resultaträkning"
                : tab === "coverage" ? "Datatäckning"
                : tab === "personnel" ? "Personalstatistik"
                : tab === "suppliers" ? "Leverantörer"
                : "Motparter"}
            </span>
          </div>

          <div className="flex items-center gap-3">
            {/* Flik-navigation */}
            <div className="flex rounded-md border border-border overflow-hidden text-sm">
              {(["pnl", "coverage", "personnel", "suppliers", "counterparties"] as Tab[]).map((t) => (
                <button
                  key={t}
                  onClick={() => setTab(t)}
                  className={`px-3 py-1.5 transition-colors ${
                    tab === t
                      ? "bg-accent text-white"
                      : "bg-surface text-fg-muted hover:bg-elevated"
                  }`}
                >
                  {t === "pnl" ? "P&L"
                    : t === "coverage" ? "Täckning"
                    : t === "personnel" ? "Personal"
                    : t === "suppliers" ? "Leverantörer"
                    : "Motparter"}
                </button>
              ))}
            </div>

            <button
              onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
              className="bg-surface border border-border rounded-md p-1.5 hover:bg-elevated transition-colors duration-180"
              aria-label="Toggle theme"
              title="Växla tema"
            >
              {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
            </button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-screen-2xl mx-auto px-6 py-6 space-y-5">
        {tab === "pnl"            && <PnlPivot />}
        {tab === "coverage"       && <CoverageReport />}
        {tab === "personnel"      && <PersonnelReport />}
        {tab === "suppliers"      && <Suppliers />}
        {tab === "counterparties" && <Counterparties />}
      </main>
    </div>
  );
}
