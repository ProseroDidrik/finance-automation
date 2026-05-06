import { useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronRight,
  ChevronsUpDown,
  ChevronUp,
  Users,
} from "lucide-react";
import {
  PersonnelCompanyRow,
  PersonnelCountry,
  PersonnelEmployee,
  PersonnelSummary,
  fetchPersonnelCountries,
  fetchPersonnelEmployees,
  fetchPersonnelSummary,
} from "../api";

type SortKey = "company_name" | "ub_latest";
type SortDir = "asc" | "desc";

const COUNTRY_LABEL: Record<string, string> = {
  Sweden: "Sverige", Norway: "Norge", Finland: "Finland",
};

const _fmt0 = new Intl.NumberFormat("sv-SE", {
  minimumFractionDigits: 0, maximumFractionDigits: 0,
});

function fmtNum(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return _fmt0.format(n);
}

function fmtPct(n: number | null | undefined): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  return `${Math.round(n * 100)}%`;
}

function fmtDate(d: string | null): string {
  return d ?? "—";
}

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <ChevronsUpDown size={12} className="text-fg-muted/50" aria-hidden />;
  return dir === "asc"
    ? <ChevronUp size={12} className="text-accent" aria-hidden />
    : <ChevronDown size={12} className="text-accent" aria-hidden />;
}

export function PersonnelReport() {
  const [countries, setCountries] = useState<PersonnelCountry[]>([]);
  const [country, setCountry] = useState<string>("Sweden");
  const [summary, setSummary] = useState<PersonnelSummary | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<number | null>(null);
  const [employees, setEmployees] = useState<Record<number, PersonnelEmployee[]>>({});
  const [empLoading, setEmpLoading] = useState<number | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>("company_name");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Initial: ladda landstillgänglighet
  useEffect(() => {
    fetchPersonnelCountries()
      .then((cs) => {
        setCountries(cs);
        if (cs.length > 0 && !cs.some((c) => c.country === country)) {
          setCountry(cs[0].country);
        }
      })
      .catch((e) => setError(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // När land ändras: hämta summary
  useEffect(() => {
    if (!country) return;
    setLoading(true);
    setError(null);
    setExpanded(null);
    fetchPersonnelSummary(country)
      .then(setSummary)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [country]);

  function toggleExpand(companyId: number) {
    if (expanded === companyId) {
      setExpanded(null);
      return;
    }
    setExpanded(companyId);
    if (!(companyId in employees)) {
      setEmpLoading(companyId);
      fetchPersonnelEmployees(companyId)
        .then((d) => setEmployees((prev) => ({ ...prev, [companyId]: d.employees })))
        .catch((e) => setError(String(e)))
        .finally(() => setEmpLoading(null));
    }
  }

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "ub_latest" ? "desc" : "asc"); }
  }

  const sortedRows = useMemo(() => {
    if (!summary) return [];
    const latest = summary.years[summary.years.length - 1];
    const ubOf = (r: PersonnelCompanyRow) => r.years[String(latest)]?.ub ?? 0;
    const out = [...summary.rows];
    out.sort((a, b) => {
      let cmp = 0;
      if (sortKey === "company_name") {
        cmp = (a.company_name ?? "").localeCompare(b.company_name ?? "", "sv");
      } else {
        cmp = ubOf(a) - ubOf(b);
      }
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [summary, sortKey, sortDir]);

  const totals = useMemo(() => {
    if (!summary) return null;
    const t: Record<string, { ub: number; began: number; slutat: number }> = {};
    for (const y of summary.years) {
      t[String(y)] = { ub: 0, began: 0, slutat: 0 };
    }
    for (const r of summary.rows) {
      for (const y of summary.years) {
        const cell = r.years[String(y)];
        if (!cell) continue;
        t[String(y)].ub     += cell.ub;
        t[String(y)].began  += cell.began;
        t[String(y)].slutat += cell.slutat;
      }
    }
    return t;
  }, [summary]);

  if (error) {
    return (
      <div role="alert" className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
        {error}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <Users size={18} aria-hidden />
          Personalstatistik
        </h1>
        <p className="text-sm text-fg-muted mt-0.5">
          Anställda per bolag och år. Klicka på en bolagsrad för individdetaljer.
        </p>
      </div>

      {/* Landväljare */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-md border border-border overflow-hidden text-xs" role="group" aria-label="Land">
          {countries.map((c) => (
            <button
              key={c.country}
              onClick={() => setCountry(c.country)}
              aria-pressed={country === c.country}
              className={`px-3 py-1.5 cursor-pointer transition-colors ${
                country === c.country
                  ? "bg-accent text-white"
                  : "bg-surface text-fg-muted hover:bg-elevated"
              }`}
            >
              {COUNTRY_LABEL[c.country] ?? c.country}
              <span className="ml-1.5 opacity-70 tabular-nums">{c.n_companies}</span>
            </button>
          ))}
        </div>
        {summary && (
          <div className="text-2xs text-fg-muted">
            Snapshot:{" "}
            {countries.find((c) => c.country === country)?.snapshot_date ?? "—"}
            {" · "}
            {summary.rows.length} bolag · {countries.find((c) => c.country === country)?.n_rows ?? 0} anställda
          </div>
        )}
      </div>

      {loading && <div className="text-fg-muted text-sm py-4">Hämtar data…</div>}

      {!loading && summary && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-xs" aria-label={`Personal ${country}`}>
            <thead>
              <tr className="border-b border-border bg-surface">
                <th className="w-6 px-2 py-2"></th>
                <th
                  onClick={() => toggleSort("company_name")}
                  className={`px-3 py-2 text-left font-medium cursor-pointer select-none whitespace-nowrap hover:bg-elevated transition-colors ${
                    sortKey === "company_name" ? "text-accent" : "text-fg-muted"
                  }`}
                  aria-sort={sortKey === "company_name" ? (sortDir === "asc" ? "ascending" : "descending") : "none"}
                >
                  <span className="inline-flex items-center gap-1">
                    Bolag
                    <SortIcon active={sortKey === "company_name"} dir={sortDir} />
                  </span>
                </th>
                {summary.years.map((y, i) => {
                  const isLast = i === summary.years.length - 1;
                  return (
                    <th
                      key={y}
                      colSpan={3}
                      className={`px-3 py-2 text-center font-medium border-l border-border ${
                        isLast ? "bg-accent/5 text-accent" : "text-fg-muted"
                      }`}
                    >
                      {y}
                    </th>
                  );
                })}
              </tr>
              <tr className="border-b border-border bg-surface text-2xs">
                <th></th>
                <th></th>
                {summary.years.flatMap((y) => [
                  <th key={`${y}-ub`}     className="px-2 py-1 text-right font-normal text-fg-muted border-l border-border">UB</th>,
                  <th key={`${y}-b`}      className="px-2 py-1 text-right font-normal text-fg-muted">Började</th>,
                  <th key={`${y}-s`}      className="px-2 py-1 text-right font-normal text-fg-muted">Slutat</th>,
                ])}
              </tr>
            </thead>
            <tbody>
              {sortedRows.length === 0 && (
                <tr>
                  <td colSpan={2 + summary.years.length * 3} className="px-3 py-8 text-center text-fg-muted">
                    Inga bolag i datasettet
                  </td>
                </tr>
              )}
              {sortedRows.map((r) => {
                const isExpanded = expanded === r.company_id;
                return (
                  <PersonnelCompanyRowView
                    key={r.company_id}
                    row={r}
                    years={summary.years}
                    expanded={isExpanded}
                    employees={employees[r.company_id] ?? null}
                    empLoading={empLoading === r.company_id}
                    onToggle={() => toggleExpand(r.company_id)}
                  />
                );
              })}

              {/* Totals */}
              {totals && (
                <tr className="border-t-2 border-border bg-surface font-semibold">
                  <td></td>
                  <td className="px-3 py-2">Totalt</td>
                  {summary.years.flatMap((y) => {
                    const t = totals[String(y)];
                    return [
                      <td key={`t-${y}-ub`}     className="px-2 py-2 text-right tabular-nums border-l border-border">{fmtNum(t.ub)}</td>,
                      <td key={`t-${y}-b`}      className="px-2 py-2 text-right tabular-nums text-positive">{t.began ? `+${fmtNum(t.began)}` : "—"}</td>,
                      <td key={`t-${y}-s`}      className="px-2 py-2 text-right tabular-nums text-negative">{t.slutat ? `−${fmtNum(t.slutat)}` : "—"}</td>,
                    ];
                  })}
                </tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function PersonnelCompanyRowView({
  row, years, expanded, employees, empLoading, onToggle,
}: {
  row: PersonnelCompanyRow;
  years: number[];
  expanded: boolean;
  employees: PersonnelEmployee[] | null;
  empLoading: boolean;
  onToggle: () => void;
}) {
  const colSpan = 2 + years.length * 3;
  return (
    <>
      <tr
        onClick={onToggle}
        className={`border-b border-border/50 cursor-pointer transition-colors ${
          expanded ? "bg-elevated" : "hover:bg-elevated"
        }`}
      >
        <td className="px-2 py-1.5">
          {expanded
            ? <ChevronDown size={12} className="text-accent" aria-hidden />
            : <ChevronRight size={12} className="text-fg-muted" aria-hidden />}
        </td>
        <td className="px-3 py-1.5 whitespace-nowrap">
          <span className="text-fg-muted">{row.company_id} · </span>
          {row.company_name ?? "—"}
        </td>
        {years.flatMap((y) => {
          const cell = row.years[String(y)];
          return [
            <td key={`${row.company_id}-${y}-ub`} className="px-2 py-1.5 text-right tabular-nums border-l border-border">
              {cell ? fmtNum(cell.ub) : "—"}
            </td>,
            <td key={`${row.company_id}-${y}-b`} className="px-2 py-1.5 text-right tabular-nums text-positive/80">
              {cell?.began ? `+${cell.began}` : ""}
            </td>,
            <td key={`${row.company_id}-${y}-s`} className="px-2 py-1.5 text-right tabular-nums text-negative/80">
              {cell?.slutat ? `−${cell.slutat}` : ""}
            </td>,
          ];
        })}
      </tr>
      {expanded && (
        <tr>
          <td colSpan={colSpan} className="px-0 py-0 bg-bg/50">
            <div className="border-l-2 border-accent/40 ml-3 my-1">
              {empLoading && (
                <div className="px-3 py-2 text-fg-muted text-2xs">Hämtar anställda…</div>
              )}
              {!empLoading && employees && (
                <EmployeeTable employees={employees} />
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function EmployeeTable({ employees }: { employees: PersonnelEmployee[] }) {
  if (employees.length === 0) {
    return <div className="px-3 py-2 text-fg-muted text-2xs">Inga anställda</div>;
  }
  // Avgör vilka kolumner som ska visas baserat på datat (NO har location, FI har lön/billable).
  const hasLocation   = employees.some((e) => e.location);
  const hasSalary     = employees.some((e) => e.salary_local !== null);
  const hasBillable   = employees.some((e) => e.billable_pct !== null);
  const hasCategory   = employees.some((e) => e.category);
  const hasProductivity = employees.some((e) => e.productivity !== null);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-2xs">
        <thead className="text-fg-muted">
          <tr className="border-b border-border/50">
            <th className="px-3 py-1.5 text-left font-medium">Namn</th>
            <th className="px-3 py-1.5 text-left font-medium">Titel</th>
            <th className="px-3 py-1.5 text-left font-medium">Anställd</th>
            <th className="px-3 py-1.5 text-left font-medium">Slut</th>
            <th className="px-3 py-1.5 text-right font-medium">%</th>
            {hasProductivity && <th className="px-3 py-1.5 text-right font-medium">Prod</th>}
            {hasBillable     && <th className="px-3 py-1.5 text-right font-medium">Fakturer.</th>}
            <th className="px-3 py-1.5 text-center font-medium">Kön</th>
            {hasCategory && <th className="px-3 py-1.5 text-left font-medium">Kategori</th>}
            {hasLocation && <th className="px-3 py-1.5 text-left font-medium">Plats</th>}
            {hasSalary   && <th className="px-3 py-1.5 text-right font-medium">Lön</th>}
            <th className="px-3 py-1.5 text-left font-medium">Avg.anledning</th>
          </tr>
        </thead>
        <tbody>
          {employees.map((e, i) => (
            <tr key={i} className="border-b border-border/30 hover:bg-elevated/50">
              <td className="px-3 py-1 whitespace-nowrap">{e.employee_name}</td>
              <td className="px-3 py-1 text-fg-muted whitespace-nowrap">{e.title ?? "—"}</td>
              <td className="px-3 py-1 tabular-nums whitespace-nowrap">{fmtDate(e.employed_from)}</td>
              <td className={`px-3 py-1 tabular-nums whitespace-nowrap ${e.employed_to ? "text-negative" : "text-positive"}`}>
                {e.employed_to ?? "Aktiv"}
              </td>
              <td className="px-3 py-1 text-right tabular-nums">{fmtPct(e.employment_pct)}</td>
              {hasProductivity && <td className="px-3 py-1 text-right tabular-nums text-fg-muted">{fmtPct(e.productivity)}</td>}
              {hasBillable     && <td className="px-3 py-1 text-right tabular-nums text-fg-muted">{fmtPct(e.billable_pct)}</td>}
              <td className="px-3 py-1 text-center text-fg-muted">{e.gender ?? "—"}</td>
              {hasCategory && <td className="px-3 py-1 text-fg-muted">{e.category ?? "—"}</td>}
              {hasLocation && <td className="px-3 py-1 text-fg-muted whitespace-nowrap">{e.location ?? "—"}</td>}
              {hasSalary   && <td className="px-3 py-1 text-right tabular-nums">{fmtNum(e.salary_local)}</td>}
              <td className="px-3 py-1 text-fg-muted whitespace-nowrap">{e.termination_reason ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
