import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import {
  CheckCircle2, XCircle, CircleSlash, AlertTriangle,
  ChevronUp, ChevronDown, ChevronsUpDown,
} from "lucide-react";
import { CoverageRow, fetchCoverage } from "../api";
import { fmtCurrency, fmtPeriod } from "../lib/format";
import { CoverageAccountsDrawer, CoverageAccountsSelection } from "./CoverageAccountsDrawer";

// ----- Typer ---------------------------------------------------------------

type StatusFilter = "all" | "missing" | "missing_zero" | "mismatch" | "ok";
type SortKey = "period" | "company_name" | "source_kind" | "status" | "backup_rows" | "backup_sum";
type SortDir = "asc" | "desc";

interface CellAggregate {
  ok: number;
  missing: number;
  missing_zero: number;
  mismatch: number;
  extra: number;
  total: number;
}

interface DrillSelection {
  country: string;
  source_kind: string;
  period?: string;  // om undefined: alla månader
}

// ----- Konstanter ----------------------------------------------------------

const STATUS_LABEL: Record<CoverageRow["status"], string> = {
  missing:      "Saknas",
  missing_zero: "Saknas (noll)",
  mismatch:     "Avvikelse",
  ok:           "OK",
  extra:        "Extra",
};

const STATUS_ICON: Record<CoverageRow["status"], ReactElement> = {
  missing:      <XCircle size={12} className="shrink-0" aria-hidden />,
  missing_zero: <CircleSlash size={12} className="shrink-0" aria-hidden />,
  mismatch:     <AlertTriangle size={12} className="shrink-0" aria-hidden />,
  ok:           <CheckCircle2 size={12} className="shrink-0" aria-hidden />,
  extra:        <AlertTriangle size={12} className="shrink-0" aria-hidden />,
};

const STATUS_CLS: Record<CoverageRow["status"], string> = {
  missing:      "bg-negative/15 text-negative",
  missing_zero: "bg-fg-muted/10 text-fg-muted",
  mismatch:     "bg-warn/15 text-warn",
  ok:           "text-positive",
  extra:        "bg-warn/15 text-warn",
};

const ROW_CLS: Record<CoverageRow["status"], string> = {
  missing:      "bg-negative/5 hover:bg-negative/10",
  missing_zero: "hover:bg-elevated",
  mismatch:     "bg-warn/5 hover:bg-warn/10",
  ok:           "hover:bg-elevated",
  extra:        "bg-warn/5 hover:bg-warn/10",
};

const STATUS_SORT_ORDER: Record<CoverageRow["status"], number> = {
  missing: 0, mismatch: 1, extra: 2, missing_zero: 3, ok: 4,
};

// Första året med inläst data i warehouse (SIE/SAF-T-historik + Mercur-backup).
const COVERAGE_START_YEAR = 2022;

function monthsForYear(year: number): string[] {
  return Array.from({ length: 12 }, (_, i) => `${year}${String(i + 1).padStart(2, "0")}`);
}

function availableYears(): number[] {
  const current = new Date().getFullYear();
  const years: number[] = [];
  for (let y = COVERAGE_START_YEAR; y <= Math.max(current, COVERAGE_START_YEAR); y++) {
    years.push(y);
  }
  return years;
}

const COUNTRY_ORDER = ["Sweden", "Norway", "Finland", "Denmark", "Germany", "CENTR", "CA"];

// Källor som inte är intressanta för täckningsöversikten:
// - MAN: Mercur-manuella poster (allokeringar, ej riktig ETL-källa)
// - IMP_ADJ: justeringslager ovanpå IMP, inte ETL-täckning per se
const HIDDEN_SOURCE_KINDS = new Set(["MAN", "IMP_ADJ"]);

// "Strukturellt brus" — kända, dokumenterade avvikelser som inte är ETL-buggar.
// Att visa dem som rött/gult döljer riktiga ETL-problem visuellt.
//
// SE-SIE: 35/49 svenska SIE-bolag saknar #PSALDO-rader → load_sie.py taggar #RES med
// --period vilket ger 5-30 % timing-brus per konto. Se memory reference_sie_psaldo.md.
//
// NO-SAFT: backup_from_mercur lagrar monthly med Mercur-konvention, fact_balances
// lagrar YTD med SIE-konvention → kräver dubbel normalisering vid jämförelse.
// account_diff-CTE gör detta men kontoplans-grovhet ger fortfarande mismatches.
//
// 'extra' (fact har data, backup saknar) för SE-SIE/NO-SAFT är samma fenomen:
// Mercur-backupen exporterar inte SIE/SAFT-källan för historiska år (2022-2025),
// så all vår SIE/SAFT-historik blir 'extra'. Inte saknad data — bara utanför facit.
function isStructuralNoise(row: CoverageRow): boolean {
  if (row.status !== "mismatch" && row.status !== "extra") return false;
  if (row.country === "Sweden" && row.source_kind === "SIE") return true;
  if (row.country === "Norway" && row.source_kind === "SAFT") return true;
  return false;
}

// ----- Hjälpare ------------------------------------------------------------

function emptyAgg(): CellAggregate {
  return { ok: 0, missing: 0, missing_zero: 0, mismatch: 0, extra: 0, total: 0 };
}

function cellColor(agg: CellAggregate): string {
  if (agg.total === 0) return "bg-surface text-fg-muted/60";
  // Räkna missing_zero som "ok" för cellfärgen — pre-allokerade tomma rader är
  // inte ett fel utan en harmlös konsekvens av hur Mercur strukturerar backupen.
  const effectiveOk = agg.ok + agg.missing_zero;
  if (agg.missing === 0 && agg.mismatch === 0 && agg.extra === 0) {
    return "bg-positive/15 text-positive hover:bg-positive/25";
  }
  if (effectiveOk === 0) return "bg-negative/20 text-negative hover:bg-negative/30";
  return "bg-warn/15 text-warn hover:bg-warn/25";
}

function SortIcon({ col, sortKey, sortDir }: { col: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  if (col !== sortKey) return <ChevronsUpDown size={12} className="text-fg-muted/50" aria-hidden />;
  return sortDir === "asc"
    ? <ChevronUp size={12} className="text-accent" aria-hidden />
    : <ChevronDown size={12} className="text-accent" aria-hidden />;
}

// ----- Komponenten --------------------------------------------------------

export function CoverageReport() {
  const [rows, setRows]       = useState<CoverageRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError]     = useState<string | null>(null);
  const [drill, setDrill]     = useState<DrillSelection | null>(null);
  const [filter, setFilter]   = useState<StatusFilter>("all");
  const [sortKey, setSortKey] = useState<SortKey>("period");
  const [sortDir, setSortDir] = useState<SortDir>("asc");
  const [accountsSelection, setAccountsSelection] = useState<CoverageAccountsSelection | null>(null);
  const [hideStructuralNoise, setHideStructuralNoise] = useState(true);
  const [selectedYear, setSelectedYear] = useState(() => new Date().getFullYear());

  const months = useMemo(() => monthsForYear(selectedYear), [selectedYear]);

  // Stabil onClose-referens så drawerns Escape-listener inte re-registreras
  // onödigt mycket när parent rendrar om.
  const closeAccountsDrawer = useCallback(() => setAccountsSelection(null), []);

  useEffect(() => {
    setLoading(true);
    // Rensa drill/drawer — de pekar på en period i tidigare valt år.
    setDrill(null);
    setAccountsSelection(null);
    fetchCoverage({ periodFrom: `${selectedYear}01`, periodTo: `${selectedYear}12` })
      .then(setRows)
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [selectedYear]);

  // Filtrera bort ointressanta lager (MAN/IMP_ADJ) — de ska inte påverka
  // matrisen, kolumnsummorna eller den övergripande sammanfattningen.
  // Dölj också "strukturellt brus" (SE-SIE/NO-SAFT mismatches pga #PSALDO-
  // frånvaro resp. YTD/sign-konvention) när toggeln är aktiv, så att
  // återstående färgade celler är riktiga ETL-problem.
  const visibleRows = useMemo(
    () => rows.filter((r) =>
      !HIDDEN_SOURCE_KINDS.has(r.source_kind) &&
      !(hideStructuralNoise && isStructuralNoise(r))
    ),
    [rows, hideStructuralNoise],
  );

  // Räkna strukturellt brus separat för att kunna visa siffran i toggeln.
  const structuralNoiseCount = useMemo(
    () => rows.filter((r) =>
      !HIDDEN_SOURCE_KINDS.has(r.source_kind) && isStructuralNoise(r)
    ).length,
    [rows],
  );

  // Bygg matrisen: { country | source_kind | period -> CellAggregate }
  const matrix = useMemo(() => {
    const m = new Map<string, Map<string, Map<string, CellAggregate>>>();
    for (const r of visibleRows) {
      const country = r.country || "—";
      const sk = r.source_kind;
      if (!m.has(country)) m.set(country, new Map());
      const skMap = m.get(country)!;
      if (!skMap.has(sk)) skMap.set(sk, new Map());
      const pMap = skMap.get(sk)!;
      if (!pMap.has(r.period)) pMap.set(r.period, emptyAgg());
      const agg = pMap.get(r.period)!;
      agg.total += 1;
      agg[r.status] += 1;
    }
    return m;
  }, [visibleRows]);

  // Sortera rader för matrix-rendering: land enligt COUNTRY_ORDER, sedan source_kind
  const matrixRows = useMemo(() => {
    const result: { country: string; source_kind: string }[] = [];
    const countries = Array.from(matrix.keys()).sort((a, b) => {
      const ia = COUNTRY_ORDER.indexOf(a); const ib = COUNTRY_ORDER.indexOf(b);
      if (ia === -1 && ib === -1) return a.localeCompare(b);
      if (ia === -1) return 1;
      if (ib === -1) return -1;
      return ia - ib;
    });
    for (const c of countries) {
      const sks = Array.from(matrix.get(c)!.keys()).sort();
      for (const sk of sks) result.push({ country: c, source_kind: sk });
    }
    return result;
  }, [matrix]);

  // Total per kolumn (alla länder, alla källor)
  const colTotals = useMemo(() => {
    const t = new Map<string, CellAggregate>();
    for (const r of visibleRows) {
      if (!t.has(r.period)) t.set(r.period, emptyAgg());
      const agg = t.get(r.period)!;
      agg.total += 1;
      agg[r.status] += 1;
    }
    return t;
  }, [visibleRows]);

  // Drill-down: filtrera till valda (country, source_kind, [period])
  const drillRows = useMemo(() => {
    if (!drill) return [] as CoverageRow[];
    let out = visibleRows.filter((r) =>
      r.country === drill.country &&
      r.source_kind === drill.source_kind &&
      (drill.period === undefined || r.period === drill.period)
    );
    if (filter !== "all") out = out.filter((r) => r.status === filter);
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
  }, [visibleRows, drill, filter, sortKey, sortDir]);

  // Övergripande summa
  const grand = useMemo(() => {
    const g = emptyAgg();
    for (const r of visibleRows) { g.total += 1; g[r.status] += 1; }
    return g;
  }, [visibleRows]);

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
    <div className="space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div>
          <h1 className="text-xl font-semibold">Datatäckning {selectedYear}</h1>
          <p className="text-sm text-fg-muted mt-0.5">
            Mercur-facit ({grand.total} förväntade kombinationer) jämfört mot fact_balances.
            Klicka en cell för att se de bolag som matchar/saknas.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm shrink-0">
          <span className="text-fg-muted">År</span>
          <select
            value={selectedYear}
            onChange={(e) => setSelectedYear(Number(e.target.value))}
            className="bg-surface border border-border rounded-md px-2 py-1 text-fg cursor-pointer"
          >
            {availableYears().map((y) => (
              <option key={y} value={y}>{y}</option>
            ))}
          </select>
        </label>
      </div>

      {/* Total-sammanfattning */}
      <div className="flex flex-wrap items-center gap-5 text-sm">
        <span className="flex items-center gap-1.5 text-positive">
          <CheckCircle2 size={14} aria-hidden /> {grand.ok} ok
        </span>
        <span className="flex items-center gap-1.5 text-warn font-semibold">
          <AlertTriangle size={14} aria-hidden /> {grand.mismatch} avvikelser
        </span>
        <span className="flex items-center gap-1.5 text-negative font-semibold">
          <XCircle size={14} aria-hidden /> {grand.missing} saknade
        </span>
        {grand.missing_zero > 0 && (
          <span className="flex items-center gap-1.5 text-fg-muted">
            <CircleSlash size={14} aria-hidden /> {grand.missing_zero} noll-rader
          </span>
        )}
        {grand.extra > 0 && (
          <span className="flex items-center gap-1.5 text-warn">
            <AlertTriangle size={14} aria-hidden /> {grand.extra} extra (utanför facit)
          </span>
        )}
        <label
          className="ml-auto flex items-center gap-2 text-fg-muted cursor-pointer select-none"
          title="SE-SIE och NO-SAFT-bolag har strukturella avvikelser mot Mercur-backupen pga #PSALDO-frånvaro respektive YTD/sign-konventionsskillnader. De är inte ETL-buggar. När toggeln är på döljs dessa för att lyfta fram riktiga problem."
        >
          <input
            type="checkbox"
            checked={hideStructuralNoise}
            onChange={(e) => setHideStructuralNoise(e.target.checked)}
            className="cursor-pointer"
          />
          <span>Dölj strukturellt brus ({structuralNoiseCount})</span>
        </label>
      </div>

      {/* Matrix: land × period --------------------------------------- */}
      <div className="overflow-x-auto rounded-lg border border-border">
        <table className="w-full text-xs" aria-label="Täckningsmatris">
          <thead>
            <tr className="border-b border-border bg-surface">
              <th className="px-3 py-2 text-left font-medium text-fg-muted">Land</th>
              <th className="px-3 py-2 text-left font-medium text-fg-muted">Källa</th>
              {months.map((p) => (
                <th key={p} className="px-2 py-2 text-center font-medium text-fg-muted whitespace-nowrap">
                  {p.slice(4)}
                </th>
              ))}
              <th className="px-3 py-2 text-right font-medium text-fg-muted">Σ</th>
            </tr>
          </thead>
          <tbody>
            {matrixRows.map(({ country, source_kind }) => {
              const skMap = matrix.get(country)!.get(source_kind)!;
              let rowOk = 0, rowMiss = 0, rowMisZero = 0, rowMis = 0, rowExtra = 0;
              return (
                <tr key={`${country}|${source_kind}`} className="border-b border-border/50">
                  <td className="px-3 py-1.5 whitespace-nowrap text-fg">{country}</td>
                  <td className="px-3 py-1.5 font-mono text-fg-muted">{source_kind}</td>
                  {months.map((p) => {
                    const agg = skMap.get(p) ?? emptyAgg();
                    rowOk += agg.ok;
                    rowMiss += agg.missing;
                    rowMisZero += agg.missing_zero;
                    rowMis += agg.mismatch;
                    rowExtra += agg.extra;
                    // missing_zero räknas som "ok" i celldisplayen — det är harmlös
                    // pre-allokering i Mercur, inte riktig saknad data.
                    const okEff = agg.ok + agg.missing_zero;
                    const cls = cellColor(agg);
                    const isActive = drill?.country === country
                      && drill?.source_kind === source_kind && drill?.period === p;
                    return (
                      <td key={p} className="p-0.5">
                        <button
                          type="button"
                          onClick={() => setDrill({ country, source_kind, period: p })}
                          disabled={agg.total === 0}
                          aria-label={`${country} ${source_kind} ${p}: ${okEff} ok, ${agg.missing} saknas, ${agg.mismatch} avvikelse`}
                          className={`w-full px-2 py-1 rounded text-center tabular-nums transition-colors
                            ${cls} ${isActive ? "ring-2 ring-accent" : ""}
                            ${agg.total > 0 ? "cursor-pointer" : "cursor-default"}`}
                          title={
                            agg.total === 0 ? "Inget i facit"
                            : `${okEff}/${agg.total} ok${agg.missing_zero ? ` (varav ${agg.missing_zero} noll-rader)` : ""}${agg.missing ? ` · ${agg.missing} saknas` : ""}${agg.mismatch ? ` · ${agg.mismatch} avvikelse` : ""}`
                          }
                        >
                          {agg.total === 0 ? "—" : `${okEff}/${agg.total}`}
                        </button>
                      </td>
                    );
                  })}
                  <td className="px-3 py-1.5 text-right tabular-nums">
                    <button
                      type="button"
                      onClick={() => setDrill({ country, source_kind })}
                      className={`px-2 py-1 rounded hover:bg-elevated
                        ${drill?.country === country && drill?.source_kind === source_kind && drill?.period === undefined ? "ring-2 ring-accent" : ""}`}
                      title="Alla månader"
                    >
                      <span className="text-positive">{rowOk + rowMisZero}</span>
                      {rowMis > 0 && <span className="text-warn"> · {rowMis}</span>}
                      {rowMiss > 0 && <span className="text-negative"> · {rowMiss}</span>}
                      {rowExtra > 0 && <span className="text-warn"> · {rowExtra}e</span>}
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
          <tfoot>
            <tr className="border-t border-border bg-surface">
              <td colSpan={2} className="px-3 py-2 text-fg-muted font-medium">Σ alla länder</td>
              {months.map((p) => {
                const agg = colTotals.get(p) ?? emptyAgg();
                const okEff = agg.ok + agg.missing_zero;
                return (
                  <td key={p} className="px-2 py-2 text-center tabular-nums text-fg-muted">
                    {agg.total === 0 ? "—" : `${okEff}/${agg.total}`}
                  </td>
                );
              })}
              <td className="px-3 py-2 text-right text-fg-muted tabular-nums">{grand.ok + grand.missing_zero}/{grand.total}</td>
            </tr>
          </tfoot>
        </table>
      </div>

      <div className="text-2xs text-fg-muted">
        Färgkodning: <span className="text-positive">grön = allt ok</span> ·{" "}
        <span className="text-warn">gul = någon avvikelse/extra</span> ·{" "}
        <span className="text-negative">röd = inget i fact_balances</span> ·{" "}
        grå = inget i facit. <CircleSlash size={10} className="inline align-text-bottom" aria-hidden />{" "}
        noll-rader (SIE/SAFT med backup-summa ≈ 0) räknas som ok eftersom Mercur
        pre-allokerar dem för bolag utan månadsbevegelse.
      </div>

      {/* Drill-down --------------------------------------------------- */}
      {drill && (
        <div className="space-y-3">
          <div className="flex items-center justify-between">
            <h2 className="text-sm font-semibold">
              Detaljer: {drill.country} · {drill.source_kind}
              {drill.period ? ` · ${fmtPeriod(drill.period)}` : " · alla månader"}
              <span className="text-fg-muted font-normal"> ({drillRows.length} rader)</span>
            </h2>
            <button
              type="button"
              onClick={() => { setDrill(null); setFilter("all"); }}
              className="text-xs text-accent hover:underline cursor-pointer"
            >
              Stäng
            </button>
          </div>

          {/* Status-filter */}
          <div className="flex rounded-md border border-border overflow-hidden text-xs w-fit" role="group" aria-label="Filtrera på status">
            {(["all", "missing", "missing_zero", "mismatch", "ok"] as StatusFilter[]).map((f) => (
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
                {f === "all"          ? "Alla"
                  : f === "missing"      ? "Saknade"
                  : f === "missing_zero" ? "Noll-rader"
                  : f === "mismatch"     ? "Avvikelser"
                  : "OK"}
              </button>
            ))}
          </div>

          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-xs" aria-label="Täckning detaljer">
              <thead>
                <tr className="border-b border-border bg-surface">
                  <th {...thProps("period", "Period")} />
                  <th {...thProps("company_name", "Bolag")} />
                  <th {...thProps("source_kind", "Källa")} />
                  <th {...thProps("backup_rows", "Facit-rader", "right")} />
                  <th className="px-3 py-2 text-right font-medium text-fg-muted whitespace-nowrap">Fact-rader</th>
                  <th {...thProps("backup_sum", "Facit-summa (k)", "right")} />
                  <th className="px-3 py-2 text-right font-medium text-fg-muted whitespace-nowrap">Fact-summa (k)</th>
                  <th {...thProps("status", "Status")} />
                </tr>
              </thead>
              <tbody>
                {drillRows.length === 0 && (
                  <tr>
                    <td colSpan={8} className="px-3 py-8 text-center text-fg-muted">
                      Inga rader matchar filtret
                    </td>
                  </tr>
                )}
                {drillRows.map((r, i) => (
                  <tr
                    key={i}
                    role="button"
                    tabIndex={0}
                    aria-label={`Visa per-konto-diff för ${r.company_name} ${r.period} ${r.source_kind}`}
                    onClick={() => setAccountsSelection({
                      company_id:   r.company_id,
                      company_name: r.company_name,
                      period:       r.period,
                      source_kind:  r.source_kind,
                    })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        setAccountsSelection({
                          company_id:   r.company_id,
                          company_name: r.company_name,
                          period:       r.period,
                          source_kind:  r.source_kind,
                        });
                      }
                    }}
                    title="Klicka för per-konto-diff"
                    className={`border-b border-border/50 transition-colors cursor-pointer ${ROW_CLS[r.status]}`}
                  >
                    <td className="px-3 py-1.5 tabular-nums whitespace-nowrap">{fmtPeriod(r.period)}</td>
                    <td className="px-3 py-1.5 whitespace-nowrap">
                      <span className="text-fg-muted">{r.company_id} · </span>
                      {r.company_name ?? "—"}
                    </td>
                    <td className="px-3 py-1.5 font-mono">{r.source_kind}</td>
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
            Summor i tusental (k) i bolagets valuta · Klicka kolumnhuvud för sortering
          </div>
        </div>
      )}

      <CoverageAccountsDrawer
        selection={accountsSelection}
        onClose={closeAccountsDrawer}
      />
    </div>
  );
}
