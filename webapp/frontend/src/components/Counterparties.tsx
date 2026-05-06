import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, CheckCircle2, ChevronDown, ChevronRight, ExternalLink,
  Play, RefreshCw, ShieldAlert, XCircle,
} from "lucide-react";
import {
  CounterpartyPeriod, CounterpartyReport, CounterpartyRow, CounterpartyRunStatus,
  fetchCounterparties, fetchCounterpartyPeriods, fetchCounterpartyRunStatus,
  startCounterpartyRun,
} from "../api";

type StatusFilter = "all" | "flagged" | "konkurs" | "avveckling" | "tvangs" | "sanctions";

const FILTER_OPTS: { value: StatusFilter; label: string }[] = [
  { value: "all",        label: "Alla"        },
  { value: "flagged",    label: "Flaggade"    },
  { value: "konkurs",    label: "Konkurs"     },
  { value: "avveckling", label: "Avveckling"  },
  { value: "tvangs",     label: "Tvangs"      },
  { value: "sanctions",  label: "Sanctions"   },
];

const BADGE_CLS: Record<string, string> = {
  KONKURS:      "bg-negative/15 text-negative",
  AVVECKLING:   "bg-warn/15 text-warn",
  TVANGS:       "bg-warn/15 text-warn",
  SANCTIONS:    "bg-negative/15 text-negative",
  "EJ I BRREG": "bg-fg-muted/15 text-fg-muted",
};

export function Counterparties() {
  const [periods, setPeriods]       = useState<CounterpartyPeriod[]>([]);
  const [period, setPeriod]         = useState<string>("");
  const [report, setReport]         = useState<CounterpartyReport | null>(null);
  const [loading, setLoading]       = useState<boolean>(false);
  const [error, setError]           = useState<string | null>(null);

  const [filter, setFilter]         = useState<StatusFilter>("flagged");
  const [search, setSearch]         = useState<string>("");
  const [expanded, setExpanded]     = useState<Set<string>>(new Set());

  // Run-state
  const [withSanctions, setWithSanctions]       = useState(false);
  const [includeCustomers, setIncludeCustomers] = useState(false);
  const [runStatus, setRunStatus] = useState<CounterpartyRunStatus | null>(null);
  const pollRef = useRef<number | null>(null);

  // Initial: ladda perioder + senaste status
  useEffect(() => {
    Promise.all([fetchCounterpartyPeriods(), fetchCounterpartyRunStatus()])
      .then(([ps, st]) => {
        setPeriods(ps);
        setRunStatus(st);
        if (ps.length > 0) {
          // Default: senaste period med CSV, annars första
          const def = ps.find((p) => p.has_csv) ?? ps[0];
          setPeriod(def.period);
        }
      })
      .catch((e) => setError(String(e)));
  }, []);

  // Hämta data när period ändras eller efter completed run
  useEffect(() => {
    if (!period) return;
    setLoading(true);
    setError(null);
    fetchCounterparties(period)
      .then(setReport)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [period]);

  // Polla status om running. Vid completion → ladda om data.
  useEffect(() => {
    if (!runStatus?.running) return;
    pollRef.current = window.setInterval(() => {
      fetchCounterpartyRunStatus()
        .then((st) => {
          setRunStatus(st);
          if (!st.running) {
            // Klar — uppdatera periodlistan + data
            fetchCounterpartyPeriods().then(setPeriods);
            if (st.period === period) {
              fetchCounterparties(period).then(setReport).catch(() => {});
            }
          }
        })
        .catch(() => {});
    }, 2000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [runStatus?.running, period]);

  function startRun() {
    if (!period) return;
    setError(null);
    startCounterpartyRun(period, withSanctions, includeCustomers)
      .then(setRunStatus)
      .catch((e) => setError(String(e)));
  }

  function toggleRow(orgnr: string) {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(orgnr)) n.delete(orgnr); else n.add(orgnr);
      return n;
    });
  }

  // Filter + sort
  const visibleRows = useMemo(() => {
    if (!report) return [] as CounterpartyRow[];
    const q = search.trim().toLowerCase();
    return report.rows
      .filter((r) => {
        switch (filter) {
          case "all":        break;
          case "flagged":    if (r.status !== "flagged") return false; break;
          case "konkurs":    if (!r.konkurs) return false; break;
          case "avveckling": if (!r.under_avvikling) return false; break;
          case "tvangs":     if (!r.tvangsavvikling) return false; break;
          case "sanctions":  if (!r.sanctions_review) return false; break;
        }
        if (q) {
          const blob = `${r.orgnr} ${r.name_saft ?? ""} ${r.name_brreg ?? ""}`.toLowerCase();
          if (!blob.includes(q)) return false;
        }
        return true;
      })
      .sort((a, b) => {
        // Flaggade först, sen orgnr
        if (a.status !== b.status) return a.status === "flagged" ? -1 : 1;
        return a.orgnr.localeCompare(b.orgnr);
      });
  }, [report, filter, search]);

  const counts = useMemo(() => {
    if (!report) return { total: 0, flagged: 0, konkurs: 0, avv: 0, tvangs: 0, san: 0 };
    let flagged = 0, konkurs = 0, avv = 0, tvangs = 0, san = 0;
    for (const r of report.rows) {
      if (r.status === "flagged") flagged++;
      if (r.konkurs) konkurs++;
      if (r.under_avvikling) avv++;
      if (r.tvangsavvikling) tvangs++;
      if (r.sanctions_review) san++;
    }
    return { total: report.rows.length, flagged, konkurs, avv, tvangs, san };
  }, [report]);

  const isRunning = runStatus?.running ?? false;
  const lastRun = runStatus && !runStatus.running && runStatus.completed_at;

  return (
    <div className="space-y-4">
      <div>
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <ShieldAlert size={18} aria-hidden />
          Motparter (Norge — Brreg + sanctions)
        </h1>
        <p className="text-sm text-fg-muted mt-0.5">
          Leverantörer/kunder från SAF-T-filer kontrolleras mot Brreg.no (konkurs/avveckling/tvangsavviklling)
          och valfritt mot OFAC/EU/FN-sanktionslistor.
        </p>
      </div>

      {/* Filter-panel */}
      <div className="bg-surface border border-border rounded-lg p-3 flex flex-wrap items-center gap-3 text-sm">
        <select
          value={period}
          onChange={(e) => setPeriod(e.target.value)}
          className="bg-surface border border-border rounded-md px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent/50 tabular-nums"
        >
          {periods.length === 0 && <option value="" disabled>Inga perioder</option>}
          {periods.map((p) => (
            <option key={p.period} value={p.period}>
              {p.period} {p.has_csv ? "✓" : "(ingen CSV)"} · {p.n_saft_files} SAF-T
            </option>
          ))}
        </select>

        {/* Status-filter */}
        <div className="flex rounded-md border border-border overflow-hidden text-xs" role="group">
          {FILTER_OPTS.map((f) => (
            <button
              key={f.value}
              onClick={() => setFilter(f.value)}
              aria-pressed={filter === f.value}
              className={`px-3 py-1.5 cursor-pointer transition-colors ${
                filter === f.value
                  ? "bg-accent text-white"
                  : "bg-surface text-fg-muted hover:bg-elevated"
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>

        {/* Search */}
        <input
          type="text"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Sök orgnr eller namn..."
          className="bg-surface border border-border rounded-md px-3 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-accent/50 min-w-[14rem]"
        />

        <div className="flex-grow" />

        {/* Run-knappen + opt-checkboxar */}
        <div className="flex items-center gap-2">
          <label className="flex items-center gap-1 text-xs text-fg-muted">
            <input
              type="checkbox"
              checked={withSanctions}
              onChange={(e) => setWithSanctions(e.target.checked)}
              disabled={isRunning}
            />
            +sanctions
          </label>
          <label className="flex items-center gap-1 text-xs text-fg-muted">
            <input
              type="checkbox"
              checked={includeCustomers}
              onChange={(e) => setIncludeCustomers(e.target.checked)}
              disabled={isRunning}
            />
            +kunder
          </label>
          <button
            onClick={startRun}
            disabled={isRunning || !period}
            className={`px-3 py-1.5 rounded-md text-xs inline-flex items-center gap-1.5 transition-colors ${
              isRunning
                ? "bg-elevated text-fg-muted cursor-not-allowed"
                : "bg-accent text-white hover:bg-accent/90"
            }`}
          >
            {isRunning
              ? <RefreshCw size={12} className="animate-spin" aria-hidden />
              : <Play size={12} aria-hidden />}
            {isRunning ? "Kör..." : "Kör check"}
          </button>
        </div>
      </div>

      {/* Run-progress */}
      {(isRunning || (runStatus && runStatus.log_tail.length > 0)) && (
        <div className="bg-surface border border-border rounded-lg overflow-hidden">
          <div className="flex items-center justify-between px-4 py-2 border-b border-border text-xs">
            <span className="font-medium text-fg-muted uppercase tracking-wider">
              {isRunning
                ? `Pågående körning · period ${runStatus?.period}`
                : runStatus
                  ? `Senaste körning · ${runStatus.period} · ${
                      runStatus.return_code === 0
                        ? "OK"
                        : runStatus.return_code !== null
                          ? `exit ${runStatus.return_code}`
                          : runStatus.error ?? "—"}`
                  : "Senaste körning"}
            </span>
            {lastRun && (
              <span className="text-fg-muted/80 text-2xs">{runStatus?.completed_at?.replace("T", " ").slice(0, 19)}</span>
            )}
          </div>
          <pre className="px-4 py-2 text-2xs max-h-48 overflow-y-auto bg-bg/50 font-mono whitespace-pre-wrap">
            {(runStatus?.log_tail ?? []).join("\n") || "(ingen output ännu)"}
          </pre>
        </div>
      )}

      {error && (
        <div role="alert" className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
          {error}
        </div>
      )}

      {/* Sammanfattning */}
      {report && report.csv_exists && (
        <div className="flex flex-wrap items-center gap-5 text-sm">
          <span className="text-fg-muted">{counts.total.toLocaleString("sv-SE")} motparter</span>
          <span className="flex items-center gap-1.5 text-negative font-semibold">
            <XCircle size={14} aria-hidden /> {counts.flagged} flaggade
          </span>
          {counts.konkurs > 0 && <span className="text-negative">⚠ {counts.konkurs} konkurs</span>}
          {counts.avv > 0     && <span className="text-warn">{counts.avv} avveckling</span>}
          {counts.tvangs > 0  && <span className="text-warn">{counts.tvangs} tvangs</span>}
          {counts.san > 0     && <span className="text-negative">{counts.san} sanctions</span>}
          <span className="text-positive flex items-center gap-1.5">
            <CheckCircle2 size={14} aria-hidden /> {counts.total - counts.flagged} ok
          </span>
        </div>
      )}

      {report && !report.csv_exists && !isRunning && (
        <div className="bg-warn/10 border border-warn/30 text-warn text-sm rounded-md p-4">
          Ingen rapport finns för {period}. Klicka <strong>Kör check</strong> för att skapa en (kan ta några minuter
          första gången pga ~4 800 Brreg-uppslag).
        </div>
      )}

      {loading && !report && <div className="text-fg-muted text-sm py-4">Hämtar data…</div>}

      {/* Tabell */}
      {report && report.csv_exists && (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-xs" aria-label="Motparter">
            <thead>
              <tr className="border-b border-border bg-surface text-fg-muted">
                <th className="w-6 px-2 py-2"></th>
                <th className="text-left px-3 py-2 font-medium">Orgnr</th>
                <th className="text-left px-3 py-2 font-medium">Namn</th>
                <th className="text-left px-3 py-2 font-medium">Land</th>
                <th className="text-left px-3 py-2 font-medium">Typ</th>
                <th className="text-left px-3 py-2 font-medium">Status</th>
                <th className="text-right px-3 py-2 font-medium">Bolag</th>
                <th className="w-6 px-2 py-2"></th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.length === 0 && (
                <tr><td colSpan={8} className="px-3 py-8 text-center text-fg-muted">Inga rader matchar filtret</td></tr>
              )}
              {visibleRows.map((r) => {
                const isExp = expanded.has(r.orgnr);
                const flagged = r.status === "flagged";
                return (
                  <FragmentRow
                    key={r.orgnr}
                    row={r}
                    expanded={isExp}
                    flagged={flagged}
                    onToggle={() => toggleRow(r.orgnr)}
                  />
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {report && report.csv_exists && (
        <div className="text-2xs text-fg-muted">
          {visibleRows.length} av {counts.total} rader · klicka rad för bolag-detaljer
        </div>
      )}
    </div>
  );
}

function FragmentRow({
  row, expanded, flagged, onToggle,
}: {
  row: CounterpartyRow;
  expanded: boolean;
  flagged: boolean;
  onToggle: () => void;
}) {
  const name = row.name_brreg || row.name_saft || "—";
  const rowCls = flagged
    ? "bg-negative/5 hover:bg-negative/10"
    : "hover:bg-elevated";
  return (
    <>
      <tr
        onClick={onToggle}
        className={`border-b border-border/50 cursor-pointer transition-colors ${rowCls}`}
      >
        <td className="px-2 py-1.5">
          {expanded
            ? <ChevronDown size={12} className="text-accent" aria-hidden />
            : <ChevronRight size={12} className="text-fg-muted" aria-hidden />}
        </td>
        <td className="px-3 py-1.5 font-mono tabular-nums whitespace-nowrap">
          <a
            href={`https://www.brreg.no/enhet/${row.orgnr}`}
            target="_blank"
            rel="noreferrer"
            onClick={(e) => e.stopPropagation()}
            className="text-accent hover:underline inline-flex items-center gap-1"
          >
            {row.orgnr}
            <ExternalLink size={10} aria-hidden />
          </a>
        </td>
        <td className="px-3 py-1.5">
          <div className="font-medium">{name}</div>
          {row.name_brreg && row.name_saft && row.name_brreg !== row.name_saft && (
            <div className="text-2xs text-fg-muted">SAF-T: {row.name_saft}</div>
          )}
        </td>
        <td className="px-3 py-1.5 text-fg-muted">{row.country ?? "—"}</td>
        <td className="px-3 py-1.5 text-fg-muted">{row.type}</td>
        <td className="px-3 py-1.5">
          <div className="flex flex-wrap gap-1">
            {row.badges.length === 0 && (
              <span className="inline-flex items-center gap-1 text-positive text-2xs">
                <CheckCircle2 size={12} aria-hidden /> ok
              </span>
            )}
            {row.badges.map((b) => (
              <span
                key={b}
                className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-2xs font-medium ${BADGE_CLS[b] ?? "bg-fg-muted/15 text-fg-muted"}`}
              >
                {b === "KONKURS" || b === "SANCTIONS" ? <XCircle size={10} aria-hidden /> :
                 b === "AVVECKLING" || b === "TVANGS" ? <AlertTriangle size={10} aria-hidden /> :
                 null}
                {b}
              </span>
            ))}
          </div>
        </td>
        <td className="px-3 py-1.5 text-right tabular-nums text-fg-muted">{row.companies.length}</td>
        <td className="px-2 py-1.5"></td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={8} className="px-0 py-0 bg-bg/50">
            <div className="border-l-2 border-accent/40 ml-3 my-1 px-3 py-2 text-2xs">
              {row.sanctions_review && (
                <div className="mb-2 text-negative">
                  <strong>Sanctions:</strong> {row.sanctions_review}
                </div>
              )}
              <div className="font-medium text-fg-muted mb-1">
                Förekommer i {row.companies.length} bolagsfil{row.companies.length === 1 ? "" : "er"}:
              </div>
              {row.companies.length === 0 ? (
                <div className="text-fg-muted">
                  (ingen bolags-mapping — fil kan ha varit otillgänglig vid SAF-T-parsning;
                  scriptet noterade <code>{row.source_file}</code>)
                </div>
              ) : (
                <ul className="space-y-0.5">
                  {row.companies.map((c, i) => (
                    <li key={i} className="flex items-center gap-2">
                      <span className="text-fg-muted tabular-nums w-10">
                        {c.company_id !== null ? String(c.company_id).padStart(3, "0") : "—"}
                      </span>
                      <span className="font-medium">{c.company_label}</span>
                      <span className="text-fg-muted/70 font-mono text-2xs truncate">{c.source_file}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}
