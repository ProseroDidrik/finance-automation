import { useEffect, useState } from "react";
import { CoverageRow, fetchCoverage } from "../api";
import { fmtCurrency, fmtPeriod } from "../lib/format";

type StatusFilter = "all" | "missing" | "mismatch";

const STATUS_LABEL: Record<CoverageRow["status"], string> = {
  missing:  "Saknas",
  mismatch: "Avvikelse",
  ok:       "OK",
};

const STATUS_CLS: Record<CoverageRow["status"], string> = {
  missing:  "bg-negative/15 text-negative font-medium",
  mismatch: "bg-warning/15 text-warning font-medium",
  ok:       "text-positive",
};

const ROW_CLS: Record<CoverageRow["status"], string> = {
  missing:  "bg-negative/5 hover:bg-negative/10",
  mismatch: "bg-warning/5 hover:bg-warning/10",
  ok:       "hover:bg-elevated",
};

export function CoverageReport() {
  const [rows, setRows]     = useState<CoverageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]   = useState<string | null>(null);
  const [filter, setFilter] = useState<StatusFilter>("missing");

  useEffect(() => {
    setLoading(true);
    fetchCoverage()
      .then(setRows)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, []);

  const counts = {
    missing:  rows.filter((r) => r.status === "missing").length,
    mismatch: rows.filter((r) => r.status === "mismatch").length,
    ok:       rows.filter((r) => r.status === "ok").length,
  };

  const visible = rows.filter((r) =>
    filter === "all" ? true : r.status === filter
  );

  if (loading) return <div className="text-fg-muted text-sm py-4">Hämtar data…</div>;
  if (error)   return (
    <div className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
      {error}
    </div>
  );

  return (
    <div className="space-y-4">
      {/* Sammanfattning */}
      <div className="flex items-center gap-6 text-sm">
        <span className="text-negative font-semibold">{counts.missing} saknade</span>
        <span className="text-warning font-semibold">{counts.mismatch} avvikelser</span>
        <span className="text-positive">{counts.ok} ok</span>
      </div>

      {/* Filterknappar */}
      <div className="flex gap-2">
        {(["missing", "mismatch", "all"] as const).map((f) => (
          <button
            key={f}
            onClick={() => setFilter(f)}
            className={`px-3 py-1 rounded text-xs font-medium border transition-colors ${
              filter === f
                ? "bg-accent text-white border-accent"
                : "bg-surface border-border text-fg-muted hover:bg-elevated"
            }`}
          >
            {f === "all" ? "Alla" : f === "missing" ? "Saknade" : "Avvikelser"}
          </button>
        ))}
      </div>

      {/* Tabell */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border bg-surface text-fg-muted">
              <th className="px-3 py-2 text-left font-medium">Land</th>
              <th className="px-3 py-2 text-left font-medium">Bolag</th>
              <th className="px-3 py-2 text-left font-medium">Period</th>
              <th className="px-3 py-2 text-left font-medium">Källa</th>
              <th className="px-3 py-2 text-left font-medium">Scen</th>
              <th className="px-3 py-2 text-right font-medium">Backup-rader</th>
              <th className="px-3 py-2 text-right font-medium">Fact-rader</th>
              <th className="px-3 py-2 text-right font-medium">Backup-summa (k)</th>
              <th className="px-3 py-2 text-right font-medium">Fact-summa (k)</th>
              <th className="px-3 py-2 text-left font-medium">Status</th>
            </tr>
          </thead>
          <tbody>
            {visible.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-6 text-center text-fg-muted">
                  Inga rader att visa
                </td>
              </tr>
            )}
            {visible.map((r, i) => (
              <tr
                key={i}
                className={`border-b border-border/50 transition-colors ${ROW_CLS[r.status]}`}
              >
                <td className="px-3 py-1.5 text-fg-muted">{r.country ?? "—"}</td>
                <td className="px-3 py-1.5">
                  <span className="text-fg-muted">{r.company_id} · </span>
                  {r.company_name ?? "—"}
                </td>
                <td className="px-3 py-1.5 tabular-nums">{fmtPeriod(r.period)}</td>
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
                  <span className={`px-1.5 py-0.5 rounded text-xs ${STATUS_CLS[r.status]}`}>
                    {STATUS_LABEL[r.status]}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="text-2xs text-fg-muted">
        {visible.length} av {rows.length} rader visas · Summor i tusental (k) i bolagets valuta
      </div>
    </div>
  );
}
