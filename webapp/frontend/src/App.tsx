import { useEffect, useMemo, useState } from "react";
import { Sun, Moon } from "lucide-react";
import {
  Company,
  Period,
  PnlReport as PnlReportData,
  fetchCompanies,
  fetchPeriods,
  fetchPnlReport,
} from "./api";
import { fmtPeriod } from "./lib/format";
import { KpiBar } from "./components/KpiBar";
import { PnlReport } from "./components/PnlReport";

export default function App() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [periods, setPeriods] = useState<Period[]>([]);
  const [companyId, setCompanyId] = useState<number | null>(null);
  const [period, setPeriod] = useState<string>("");
  const [report, setReport] = useState<PnlReportData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [theme, setTheme] = useState<"dark" | "light">("light");

  useEffect(() => {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }, [theme]);

  // Initial: ladda bolag + perioder
  useEffect(() => {
    Promise.all([fetchCompanies(), fetchPeriods()])
      .then(([cs, ps]) => {
        setCompanies(cs);
        setPeriods(ps);
        // Default-val: bolag 76 om finns, annars första; senaste perioden
        const defaultCompany = cs.find((c) => c.company_id === 76) ?? cs[0];
        if (defaultCompany) setCompanyId(defaultCompany.company_id);
        if (ps[0]) setPeriod(ps[0].period);
      })
      .catch((e) => setError(String(e)));
  }, []);

  // När bolag/period ändras: ladda rapport
  useEffect(() => {
    if (companyId === null || !period) return;
    setLoading(true);
    setError(null);
    fetchPnlReport(companyId, period)
      .then(setReport)
      .catch((e) => {
        setError(String(e));
        setReport(null);
      })
      .finally(() => setLoading(false));
  }, [companyId, period]);

  const groupedCompanies = useMemo(() => {
    const groups = new Map<string, Company[]>();
    for (const c of companies) {
      const arr = groups.get(c.country) ?? [];
      arr.push(c);
      groups.set(c.country, arr);
    }
    return Array.from(groups.entries()).sort();
  }, [companies]);

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
              Resultaträkning
            </span>
          </div>

          <div className="flex items-center gap-3">
            <select
              value={companyId ?? ""}
              onChange={(e) => setCompanyId(parseInt(e.target.value, 10))}
              className="bg-surface border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/50"
            >
              {groupedCompanies.map(([country, cs]) => (
                <optgroup key={country} label={country}>
                  {cs.map((c) => (
                    <option key={c.company_id} value={c.company_id}>
                      {c.company_id} · {c.name}
                    </option>
                  ))}
                </optgroup>
              ))}
            </select>

            <select
              value={period}
              onChange={(e) => setPeriod(e.target.value)}
              className="bg-surface border border-border rounded-md px-3 py-1.5 text-sm focus:outline-none focus:ring-2 focus:ring-accent/50"
            >
              {periods.map((p) => (
                <option key={p.period} value={p.period}>
                  {fmtPeriod(p.period)}
                </option>
              ))}
            </select>

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
        {error && (
          <div className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
            {error}
          </div>
        )}

        {loading && !report && (
          <div className="text-fg-muted text-sm">Hämtar data…</div>
        )}

        {report && (
          <>
            <div className="flex items-baseline justify-between">
              <div>
                <h1 className="text-xl font-semibold">
                  {report.company.name}
                </h1>
                <div className="text-sm text-fg-muted">
                  Bolag {report.company.company_id} · {report.company.country} · {report.company.currency}
                  {" · "}
                  {fmtPeriod(report.period)}
                </div>
              </div>
              {loading && <span className="text-fg-muted text-xs">uppdaterar…</span>}
            </div>

            <KpiBar kpis={report.kpis} currency={report.company.currency} />

            <PnlReport data={report} />

            <div className="text-2xs text-fg-muted">
              Källa: {report.company.country === "Sweden" ? "SIE" : report.company.country === "Norway" ? "SAF-T" : "INL"} ·
              {" "}YTD från {fmtPeriod(report.year_start)} t.o.m. {fmtPeriod(report.period)}
            </div>
          </>
        )}
      </main>
    </div>
  );
}
