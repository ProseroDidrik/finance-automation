import { useEffect, useMemo, useState, type ReactElement } from "react";
import { CheckCircle2, XCircle, AlertTriangle, ChevronUp, ChevronDown, ChevronsUpDown } from "lucide-react";
import { CoverageRow, fetchCoverage } from "../api";
import { fmtCurrency, fmtPeriod } from "../lib/format";

type StatusFilter = "all" | "missing" | "mismatch";
type SortKey = "period" | "company_name" | "source_kind" | "status" | "backup_rows" | "backup_sum";
type SortDir = "asc" | "desc";

const STATUS_LABEL: Record<CoverageRow["status"], string> = {
  missing:  "Saknas",
  mismatch: "Avvikelse",
  ok:       "OK",
};

const STATUS_ICON: Record<CoverageRow["status"], ReactElement> = {
  missing:  <XCircle size={12} className="shrink-0" aria-hidden />,
  mismatch: <AlertTriangle size={12} className="shrink-0" aria-hidden />,
  ok:       <CheckCircle2 size={12} className="shrink-0" aria-hidden />,
};

const STATUS_CLS: Record<CoverageRow["status"], string> = {
  missing:  "bg-negative/15 text-negative",
  mismatch: "bg-warn/15 text-warn",
  ok:       "text-positive",
};

const ROW_CLS: Record<CoverageRow["status"], string> = {
  missing:  "bg-negative/5 hover:bg-negative/10",
  mismatch: "bg-warn/5 hover:bg-warn/10",
  ok:       "hover:bg-elevated",
};

const STATUS_SORT_ORDER: Record<CoverageRow["status"], number> = {
  missing: 0, mismatch: 1, ok: 2,
};

function SortIcon({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  if (col !== sortKey) return <ChevronsUpDown size={12} className="text-fg-muted/50" aria-hidden />;
  return sortDir === "asc"
    ? <ChevronUp size={12} className="text-accent" aria-hidden />
    : <ChevronDown size={12} className="text-accent" aria-hidden />;
}

export function CoverageReport() {
  const [rows, setRows]       = useState<CoverageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);
  const [filter, setFilter]   = useState<StatusFilter>("missing");
  const [country, setCountry] = useState<string>("all");
  const [sortKey, setSortKey] = useState<SortKey>("period");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  useEffect(() => {
    setLoading(true);
    fetchCoverage()
      .then(setRows)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const countries = useMemo(() =>
    ["all", ...Array.from(new Set(rows.map((r) => r.country).filter(Boolean))).sort()],
    [rows]
  );

  const counts = useMemo(() => ({
    missing:  rows.filter((r) => r.status === "missing").length,
    mismatch: rows.filter((r) => r.status === "mismatch").length,
    ok:       rows.filter((r) => r.status === "ok").length,
  }), [rows]);

  const visible = useMemo(() => {
    let out = rows.filter((r) => {
      const statusOk  = filter === "all" || r.status === filter;
      const countryOk = country === "all" || r.country === country;
      return statusOk && countryOk;
    });
    out = [...out].sort((a, b) => {
      let va: string | number, vb: string | number;
      switch (sortKey) {
        case "period":       va = a.period;        vb = b.period;        break;
        case "company_name": va = a.company_name ?? ""; vb = b.company_name ?? ""; break;
        case "source_kind":  va = a.source_kind;   vb = b.source_kind;   break;
        case "status":       va = STATUS_SORT_ORDER[a.status]; vb = STATUS_SORT_ORDER[b.status]; break;
        case "backup_rows":  va = a.backup_rows ?? -1; vb = b.backup_rows ?? -1; break;
        case "backup_sum":   va = a.backup_sum ?? 0;   vb = b.backup_sum ?? 0;   break;
        default:             return 0;
      }
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
    return out;
  }, [rows, filter, country, sortKey, sortDir]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("asc"); }
  }

  function thProps(key: SortKey, label: string, align: "left" | "right" = "left") {
    const isActive = sortKey === key;
    const ariaSort = isActive ? (sortDir === "asc" ? "ascending" : "descending") : "none";
    return {
      onClick: () => toggleSort(key),
      "aria-sort": ariaSort as "none" | "ascending" | "descending",
      className: `px-3 py-2 font-medium cursor-pointer select-none whitespace-nowrap
        hover:bg-elevated transition-colors ${align === "right" ? "text-right" : "text-left"}
        ${isActive ? "text-accent" : "text-fg-muted"}`,
      children: (
        <span className={`inline-flex items-center gap-1 ${align === "right" ? "justify-end" : ""}`}>
          {label}
          <SortIcon col={key} sortKey={sortKey} sortDir={sortDir} />
        </span>
      ),
    };
  }

  if (loading) return <div className="text-fg-muted text-sm py-4">Hämtar data…</div>;
  if (error)   return (
    <div role="alert" className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
      {error}
    </div>
  );

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold">Datatäckning</h1>
        <p className="text-sm text-fg-muted mt-0.5">Backup (Mercur) jämfört mot fact_balances — MAN / IMP / IMP_ADJ</p>
      </div>

      {/* Sammanfattning */}
      <div className="flex flex-wrap items-center gap-5 text-sm">
        <span className="flex items-center gap-1.5 text-negative font-semibold">
          <XCircle size={14} aria-hidden /> {counts.missing} saknade
        </span>
        <span className="flex items-center gap-1.5 text-warn font-semibold">
          <AlertTriangle size={14} aria-hidden /> {counts.mismatch} avvikelser
        </span>
        <span className="flex items-center gap-1.5 text-positive">
          <CheckCircle2 size={14} aria-hidden /> {counts.ok} ok
        </span>
      </div>

      {/* Filter-rad */}
      <div className="flex flex-wrap gap-2 items-center">
        {/* Status-filter */}
        <div className="flex rounded-md border border-border overflow-hidden text-xs" role="group" aria-label="Filtrera på status">
          {(["missing", "mismatch", "all"] as StatusFilter[]).map((f) => (
            <button
              key={f}
              onClick={() => setFilter(f)}
              aria-pressed={filter === f}
              className={`px-3 py-1.5 cursor-pointer transition-colors ${
                filter === f
                  ? "bg-accent text-white"
                  : "bg-surface text-fg-muted hover:bg-elevated"
              }`}
            >
              {f === "all" ? "Alla" : f === "missing" ? "Saknade" : "Avvikelser"}
            </button>
          ))}
        </div>

        {/* Land-filter */}
        <select
          value={country}
          onChange={(e) => setCountry(e.target.value)}
          aria-label="Filtrera på land"
          className="bg-surface border border-border rounded-md px-2 py-1.5 text-xs cursor-pointer
            focus:outline-none focus:ring-2 focus:ring-accent/50 text-fg"
        >
          {countries.map((c) => (
            <option key={c} value={c}>{c === "all" ? "Alla länder" : c}</option>
          ))}
        </select>
      </div>

      {/* Tabell */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs" aria-label="Täckningsrapport">
          <thead>
            <tr className="border-b border-border bg-surface">
              <th {...thProps("period", "Period")} />
              <th {...thProps("company_name", "Bolag")} />
              <th className="px-3 py-2 text-left font-medium text-fg-muted whitespace-nowrap">Land</th>
              <th {...thProps("source_kind", "Källa")} />
              <th className="px-3 py-2 text-center font-medium text-fg-muted">Scen</th>
              <th {...thProps("backup_rows", "Backup-rader", "right")} />
              <th className="px-3 py-2 text-right font-medium text-fg-muted whitespace-nowrap">Fact-rader</th>
              <th {...thProps("backup_sum", "Backup-summa (k)", "right")} />
              <th className="px-3 py-2 text-right font-medium text-fg-muted whitespace-nowrap">Fact-summa (k)</th>
              <th {...thProps("status", "Status")} />
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-8 text-center text-fg-muted">
                  Inga rader matchar filtret
                </td>
              </tr>
            )}
            {visible.map((r, i) => (
              <tr
                key={i}
                className={`border-b border-border/50 transition-colors ${ROW_CLS[r.status]}`}
              >
                <td className="px-3 py-1.5 tabular-nums whitespace-nowrap">{fmtPeriod(r.period)}</td>
                <td className="px-3 py-1.5 whitespace-nowrap">
                  <span className="text-fg-muted">{r.company_id} · </span>
                  {r.company_name ?? "—"}
                </td>
                <td className="px-3 py-1.5 text-fg-muted whitespace-nowrap">{r.country ?? "—"}</td>
                <td className="px-3 py-1.5 font-mono">{r.source_kind}</td>
                <td className="px-3 py-1.5 text-center">{r.scenario}</td>
                <td className="px-3 py-1.5 text-right tabular-nums text-fg-muted">
                  {r.backup_rows ?? "—"}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums text-fg-muted">
                  {r.fact_rows ?? "—"}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {fmtCurrency(r.backup_sum)}
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {fmtCurrency(r.fact_sum)}
                </td>
                <td className="px-3 py-1.5">
                  <span
                    className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium ${STATUS_CLS[r.status]}`}
                    aria-label={STATUS_LABEL[r.status]}
                  >
                    {STATUS_ICON[r.status]}
                    {STATUS_LABEL[r.status]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="text-2xs text-fg-muted">
        {visible.length} av {rows.length} rader · Summor i tusental (k) i bolagets valuta ·
        Klicka kolumnhuvud för sortering
      </div>
    </div>
  );
}
