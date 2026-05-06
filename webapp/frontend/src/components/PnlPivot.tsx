import { useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight, Eye, EyeOff } from "lucide-react";
import {
  Company, Granularity, PivotKpi, PivotReport, PivotRow, ReportCurrency,
  fetchCompanies, fetchPeriods, fetchPivot,
} from "../api";
import { fmtBucket, fmtCurrency, fmtPercent } from "../lib/format";

// Display-rad: tree-nod ELLER KPI-rad
type DisplayItem =
  | { kind: "tree"; row: PivotRow; hasChildren: boolean }
  | { kind: "kpi"; kpi: PivotKpi; anchorAccountId: string; anchorDepth: number };

/** Bygg display-listan: tree-rader i sort_path-ordning + KPI-rader infogade
 *  EFTER subträdet under sin anchor. Samma mönster som PnlReport.tsx. */
function buildDisplay(rows: PivotRow[], kpis: PivotKpi[]): DisplayItem[] {
  // childCount per parent_id — för expand-affordance
  const childCount = new Map<string, number>();
  for (const r of rows) {
    if (r.parent_id) childCount.set(r.parent_id, (childCount.get(r.parent_id) ?? 0) + 1);
  }
  // KPI:er per anchor
  const kpiByAnchor = new Map<string, PivotKpi[]>();
  for (const k of kpis) {
    const arr = kpiByAnchor.get(k.anchor) ?? [];
    arr.push(k);
    kpiByAnchor.set(k.anchor, arr);
  }
  // index per account_id för depth-uppslag
  const rowById = new Map<string, PivotRow>();
  for (const r of rows) rowById.set(r.account_id, r);

  const result: DisplayItem[] = [];
  type Open = { account_id: string; depth: number };
  const open: Open[] = [];

  function flushClosed(currentDepth: number) {
    while (open.length > 0 && currentDepth <= open[open.length - 1].depth) {
      const a = open.pop()!;
      const ks = kpiByAnchor.get(a.account_id);
      if (ks) {
        for (const k of ks) {
          result.push({
            kind: "kpi", kpi: k,
            anchorAccountId: a.account_id,
            anchorDepth: a.depth,
          });
        }
      }
    }
  }

  for (const r of rows) {
    flushClosed(r.depth);
    result.push({
      kind: "tree", row: r,
      hasChildren: (childCount.get(r.account_id) ?? 0) > 0,
    });
    if (kpiByAnchor.has(r.account_id)) {
      open.push({ account_id: r.account_id, depth: r.depth });
    }
  }
  flushClosed(0);
  return result;
}

type ScopeKind = "company" | "country";
interface Scope {
  kind: ScopeKind;
  companyId?: number;          // när kind='company'
  country?: string;            // när kind='country'
  label: string;
}

const GRANULARITY_OPTS: { value: Granularity; label: string }[] = [
  { value: "month",   label: "Månad"   },
  { value: "quarter", label: "Kvartal" },
  { value: "half",    label: "Halvår"  },
  { value: "year",    label: "År"      },
];

const CURRENCY_OPTS: { value: ReportCurrency; label: string }[] = [
  { value: "LOCAL", label: "Lokal valuta" },
  { value: "SEK",   label: "SEK"          },
];

function isBudgetBucket(key: string): boolean { return key.endsWith(":B"); }

function bucketAccentClass(b: { key: string; granularity: string }, kind: "header" | "cell"): string {
  if (isBudgetBucket(b.key)) {
    return kind === "header" ? "bg-warn/10 text-warn" : "bg-warn/5 italic";
  }
  if (b.granularity === "ltm") {
    return kind === "header" ? "bg-accent/10 text-accent" : "bg-accent/5";
  }
  if (b.granularity === "ytd") {
    return kind === "header" ? "bg-positive/10 text-positive" : "bg-positive/5";
  }
  return "";
}

/** Mergea budget-rapport (scenario B) in i utfall-rapporten (scenario A) genom att
 *  suffix:a budget-bucket-keys med ":B" så de inte krockar. Frontend renderar
 *  dem som kolumner med "(B)" i etiketten. */
function mergeBudgetIntoActual(actual: PivotReport, budget: PivotReport): PivotReport {
  const budgetBuckets = budget.buckets.map((b) => ({
    ...b, key: `${b.key}:B`, label: `${b.label} (B)`,
  }));

  const remap = (
    bcMap: Record<string, Record<string, number | null>>,
  ): Record<string, Record<string, number | null>> => {
    const out: Record<string, Record<string, number | null>> = {};
    for (const cid of Object.keys(bcMap)) {
      const inner: Record<string, number | null> = {};
      for (const k of Object.keys(bcMap[cid])) inner[`${k}:B`] = bcMap[cid][k];
      out[cid] = inner;
    }
    return out;
  };

  // Mergea rader: för varje budget-rad, lägg in dess by_company-data i motsvarande
  // actual-rad under budget-keys. Skapa raden om den saknas i actual.
  const rowByAcc = new Map(actual.rows.map((r) => [r.account_id, r] as const));
  const mergedRows: PivotRow[] = actual.rows.map((r) => ({ ...r, by_company: { ...r.by_company } }));
  for (const br of budget.rows) {
    let target = rowByAcc.get(br.account_id);
    if (!target) {
      // Helt nytt konto i budget — lägg till
      const fresh: PivotRow = { ...br, by_company: {} };
      mergedRows.push(fresh);
      rowByAcc.set(br.account_id, fresh);
      target = fresh;
    }
    const targetIdx = mergedRows.findIndex((x) => x.account_id === br.account_id);
    const remapped = remap(br.by_company);
    const merged = { ...mergedRows[targetIdx].by_company };
    for (const cid of Object.keys(remapped)) {
      merged[cid] = { ...(merged[cid] ?? {}), ...remapped[cid] };
    }
    mergedRows[targetIdx] = { ...mergedRows[targetIdx], by_company: merged };
  }

  // Mergea KPI:er
  const kpiByIdActual = new Map(actual.kpis.map((k) => [k.id, k] as const));
  const mergedKpis = actual.kpis.map((k) => ({ ...k, by_company: { ...k.by_company } }));
  for (const bk of budget.kpis) {
    const remapped = remap(bk.by_company);
    let target = kpiByIdActual.get(bk.id);
    if (!target) {
      mergedKpis.push({ ...bk, by_company: remapped });
      continue;
    }
    const idx = mergedKpis.findIndex((x) => x.id === bk.id);
    const merged = { ...mergedKpis[idx].by_company };
    for (const cid of Object.keys(remapped)) {
      merged[cid] = { ...(merged[cid] ?? {}), ...remapped[cid] };
    }
    mergedKpis[idx] = { ...mergedKpis[idx], by_company: merged };
  }

  return {
    ...actual,
    buckets: [...actual.buckets, ...budgetBuckets],
    rows:    mergedRows,
    kpis:    mergedKpis,
  };
}

// Default tidsspann: senaste året från senaste tillgängliga period.
function defaultPeriodRange(latest?: string): [string, string] {
  if (!latest || !/^\d{6}$/.test(latest)) return ["202401", "202412"];
  const y = parseInt(latest.slice(0, 4), 10);
  const m = parseInt(latest.slice(4), 10);
  // Hela årets perioder fram t.o.m. latest
  const from = `${y}01`;
  const to = `${y}${String(m).padStart(2, "0")}`;
  return [from, to];
}

export function PnlPivot() {
  const [companies, setCompanies] = useState<Company[]>([]);
  const [allPeriods, setAllPeriods] = useState<string[]>([]);
  const [scope, setScope] = useState<Scope | null>(null);
  const [periodFrom, setPeriodFrom] = useState<string>("");
  const [periodTo, setPeriodTo] = useState<string>("");
  const [granularity, setGranularity] = useState<Granularity>("quarter");
  const [reportCurrency, setReportCurrency] = useState<ReportCurrency>("LOCAL");
  const [includeLtm, setIncludeLtm] = useState<boolean>(false);
  const [includeYtd, setIncludeYtd] = useState<boolean>(true);
  const [includeBudget, setIncludeBudget] = useState<boolean>(false);
  const [report, setReport] = useState<PivotReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Tabellstate
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["P&L"]));
  const [hiddenBuckets, setHiddenBuckets] = useState<Set<string>>(new Set());
  const [showColumnPicker, setShowColumnPicker] = useState<boolean>(false);

  // Initial: ladda bolag + perioder
  useEffect(() => {
    Promise.all([fetchCompanies(), fetchPeriods()])
      .then(([cs, ps]) => {
        setCompanies(cs);
        const periods = ps.map((p) => p.period).sort();
        setAllPeriods(periods);
        const [f, t] = defaultPeriodRange(periods[periods.length - 1]);
        setPeriodFrom(f);
        setPeriodTo(t);
        // Default scope: 1:a tillgängliga bolaget
        const def = cs.find((c) => c.company_id === 76) ?? cs[0];
        if (def) {
          setScope({
            kind: "company",
            companyId: def.company_id,
            label: `${def.company_id} · ${def.name}`,
          });
        }
      })
      .catch((e) => setError(String(e)));
  }, []);

  // Hämta rapport när parametrar ändras
  useEffect(() => {
    if (!scope || !periodFrom || !periodTo) return;
    setLoading(true);
    setError(null);
    setReport(null);

    const baseQuery = {
      country:        scope.kind === "country" ? scope.country : undefined,
      company_ids:    scope.kind === "company" && scope.companyId ? [scope.companyId] : undefined,
      period_from:    periodFrom,
      period_to:      periodTo,
      granularity,
      report_currency: reportCurrency,
      include_ltm:    includeLtm,
      include_ytd:    includeYtd,
    } as const;

    const actualP = fetchPivot({ ...baseQuery, scenario: "A" });
    const budgetP = includeBudget
      ? fetchPivot({
          ...baseQuery,
          // För budget: bara YTD-bucket räcker normalt; vi kör samma granularity
          // som utfall så användaren får jämförbara kolumner. Källa MAN, scenario B.
          scenario:    "B",
          source_kind: "MAN",
        })
      : null;

    Promise.all([actualP, budgetP])
      .then(([actual, budget]) => {
        const merged = budget ? mergeBudgetIntoActual(actual, budget) : actual;
        setReport(merged);
        setHiddenBuckets((prev) => {
          const valid = new Set(merged.buckets.map((b) => b.key));
          const next = new Set<string>();
          for (const k of prev) if (valid.has(k)) next.add(k);
          return next;
        });
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [scope, periodFrom, periodTo, granularity, reportCurrency,
      includeLtm, includeYtd, includeBudget]);

  const groupedCompanies = useMemo(() => {
    const groups = new Map<string, Company[]>();
    for (const c of companies) {
      const arr = groups.get(c.country) ?? [];
      arr.push(c);
      groups.set(c.country, arr);
    }
    return Array.from(groups.entries()).sort();
  }, [companies]);

  const visibleBuckets = useMemo(
    () => report?.buckets.filter((b) => !hiddenBuckets.has(b.key)) ?? [],
    [report, hiddenBuckets],
  );

  // Cell-värde: summa över valda bolag (för kind='country' summeras automatiskt
  // över alla returnerade bolag; för 'company' finns bara ett bolag).
  function cellAmount(row: PivotRow, bucketKey: string): number | null {
    let total = 0;
    let any = false;
    for (const cid of Object.keys(row.by_company)) {
      const v = row.by_company[cid]?.[bucketKey];
      if (v !== null && v !== undefined) {
        total += v;
        any = true;
      }
    }
    return any ? total : null;
  }

  // Bygg display-list (tree + KPI:er) en gång per rapport
  const display = useMemo(
    () => (report ? buildDisplay(report.rows, report.kpis) : []),
    [report],
  );

  // Index för parent-chain-uppslag (synlighetsfilter)
  const rowById = useMemo(() => {
    const m = new Map<string, PivotRow>();
    if (report) for (const r of report.rows) m.set(r.account_id, r);
    return m;
  }, [report]);

  // En tree-rad är synlig om alla parents är expanded
  function isAccountVisible(accountId: string): boolean {
    const row = rowById.get(accountId);
    if (!row) return false;
    if (row.depth === 0) return true;
    let pid = row.parent_id;
    while (pid) {
      if (!expanded.has(pid)) return false;
      pid = rowById.get(pid)?.parent_id ?? null;
    }
    return true;
  }

  function toggleExpand(id: string) {
    setExpanded((prev) => {
      const n = new Set(prev);
      if (n.has(id)) n.delete(id);
      else n.add(id);
      return n;
    });
  }

  function expandAll() {
    if (!report) return;
    const s = new Set<string>(["P&L"]);
    for (const r of report.rows) if (r.is_aggregated) s.add(r.account_id);
    setExpanded(s);
  }
  function collapseAll() { setExpanded(new Set(["P&L"])); }

  function toggleBucket(key: string) {
    setHiddenBuckets((prev) => {
      const n = new Set(prev);
      if (n.has(key)) n.delete(key);
      else n.add(key);
      return n;
    });
  }

  return (
    <div className="space-y-4">
      {/* Filter-panel */}
      <div className="bg-surface border border-border rounded-lg p-3 flex flex-wrap items-center gap-3 text-sm">
        {/* Bolag/Land-väljare */}
        <select
          value={scope ? (scope.kind === "country" ? `c:${scope.country}` : `b:${scope.companyId}`) : ""}
          onChange={(e) => {
            const v = e.target.value;
            if (v.startsWith("c:")) {
              const country = v.slice(2);
              setScope({ kind: "country", country, label: `Hela ${country}` });
            } else if (v.startsWith("b:")) {
              const id = parseInt(v.slice(2), 10);
              const c = companies.find((x) => x.company_id === id);
              if (c) setScope({ kind: "company", companyId: id, label: `${c.company_id} · ${c.name}` });
            }
          }}
          className="bg-surface border border-border rounded-md px-3 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent/50 min-w-[14rem]"
        >
          <optgroup label="Konsoliderat">
            {groupedCompanies.map(([country]) => (
              <option key={`c:${country}`} value={`c:${country}`}>Hela {country}</option>
            ))}
          </optgroup>
          {groupedCompanies.map(([country, cs]) => (
            <optgroup key={country} label={country}>
              {cs.map((c) => (
                <option key={c.company_id} value={`b:${c.company_id}`}>
                  {c.company_id} · {c.name}
                </option>
              ))}
            </optgroup>
          ))}
        </select>

        {/* Period range */}
        <div className="flex items-center gap-1">
          <select
            value={periodFrom}
            onChange={(e) => setPeriodFrom(e.target.value)}
            className="bg-surface border border-border rounded-md px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent/50 tabular-nums"
            aria-label="Från period"
          >
            {allPeriods.map((p) => (<option key={p} value={p}>{p}</option>))}
          </select>
          <span className="text-fg-muted">→</span>
          <select
            value={periodTo}
            onChange={(e) => setPeriodTo(e.target.value)}
            className="bg-surface border border-border rounded-md px-2 py-1.5 focus:outline-none focus:ring-2 focus:ring-accent/50 tabular-nums"
            aria-label="Till period"
          >
            {allPeriods.map((p) => (<option key={p} value={p}>{p}</option>))}
          </select>
        </div>

        {/* Granularity */}
        <div className="flex rounded-md border border-border overflow-hidden text-xs" role="group" aria-label="Granularitet">
          {GRANULARITY_OPTS.map((g) => (
            <button
              key={g.value}
              onClick={() => setGranularity(g.value)}
              aria-pressed={granularity === g.value}
              className={`px-3 py-1.5 cursor-pointer transition-colors ${
                granularity === g.value
                  ? "bg-accent text-white"
                  : "bg-surface text-fg-muted hover:bg-elevated"
              }`}
            >
              {g.label}
            </button>
          ))}
        </div>

        {/* Rapportvaluta */}
        <select
          value={reportCurrency}
          onChange={(e) => setReportCurrency(e.target.value as ReportCurrency)}
          className="bg-surface border border-border rounded-md px-2 py-1.5 text-xs focus:outline-none focus:ring-2 focus:ring-accent/50"
          aria-label="Rapportvaluta"
        >
          {CURRENCY_OPTS.map((c) => (<option key={c.value} value={c.value}>{c.label}</option>))}
        </select>

        {/* YTD / LTM / Budget toggles */}
        <div className="flex rounded-md border border-border overflow-hidden text-xs" role="group" aria-label="Extra kolumner">
          {[
            { key: "ytd",    on: includeYtd,    set: setIncludeYtd,
              label: "YTD",    title: "Year-to-date — januari till och med vald slutperiod" },
            { key: "ltm",    on: includeLtm,    set: setIncludeLtm,
              label: "LTM",    title: "Last Twelve Months — senaste 12 månaderna" },
            { key: "budget", on: includeBudget, set: setIncludeBudget,
              label: "Budget", title: "Lägg till budget-kolumner (scenario B, källa MAN)" },
          ].map((t) => (
            <button
              key={t.key}
              onClick={() => t.set((v: boolean) => !v)}
              aria-pressed={t.on}
              title={t.title}
              className={`px-3 py-1.5 cursor-pointer transition-colors ${
                t.on
                  ? "bg-accent text-white"
                  : "bg-surface text-fg-muted hover:bg-elevated"
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        {/* Kolumn-visibility */}
        {report && report.buckets.length > 0 && (
          <div className="relative">
            <button
              onClick={() => setShowColumnPicker((v) => !v)}
              className="px-3 py-1.5 rounded-md border border-border bg-surface text-fg-muted text-xs hover:bg-elevated inline-flex items-center gap-1"
              title="Visa/dölj kolumner"
            >
              {hiddenBuckets.size > 0 ? <EyeOff size={12} aria-hidden /> : <Eye size={12} aria-hidden />}
              Kolumner ({report.buckets.length - hiddenBuckets.size}/{report.buckets.length})
            </button>
            {showColumnPicker && (
              <div className="absolute right-0 mt-1 z-20 bg-surface border border-border rounded-md shadow-lg p-2 min-w-[10rem] text-xs">
                {report.buckets.map((b) => (
                  <label key={b.key} className="flex items-center gap-2 px-2 py-1 hover:bg-elevated rounded cursor-pointer">
                    <input
                      type="checkbox"
                      checked={!hiddenBuckets.has(b.key)}
                      onChange={() => toggleBucket(b.key)}
                    />
                    <span>{fmtBucket(b.key)}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
        )}

        <div className="flex-grow" />

        <div className="flex gap-3 text-xs text-fg-muted">
          <button onClick={expandAll}   className="hover:text-accent">Expand all</button>
          <button onClick={collapseAll} className="hover:text-accent">Collapse</button>
        </div>
      </div>

      {error && (
        <div role="alert" className="bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
          {error}
        </div>
      )}

      {loading && !report && (
        <div className="text-fg-muted text-sm py-4">Hämtar rapport…</div>
      )}

      {report && (
        <>
          <div className="text-2xs text-fg-muted">
            {scope?.label} · {report.granularity} ·{" "}
            {visibleBuckets.length} kolumn{visibleBuckets.length === 1 ? "" : "er"}{" "}
            · belopp i {report.report_currency === "SEK" ? "SEK" : "lokal valuta"} (tusental)
            {loading && <span className="ml-2">uppdaterar…</span>}
          </div>

          <div className="border border-border rounded-lg overflow-x-auto bg-surface">
            <table className="w-full text-sm">
              <thead className="bg-elevated text-fg-muted text-2xs uppercase tracking-wider sticky top-0 z-10">
                <tr>
                  <th className="text-left px-4 py-2 font-medium sticky left-0 bg-elevated z-20" style={{ minWidth: "20rem" }}>
                    Konto / grupp
                  </th>
                  {visibleBuckets.map((b) => (
                    <th
                      key={b.key}
                      className={`text-right px-3 py-2 font-medium whitespace-nowrap ${
                        bucketAccentClass(b, "header")
                      }`}
                    >
                      {b.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {display.length === 0 && (
                  <tr>
                    <td
                      colSpan={visibleBuckets.length + 1}
                      className="px-4 py-8 text-center text-fg-muted"
                    >
                      Ingen data för valt urval
                    </td>
                  </tr>
                )}
                {display.map((d, i) => {
                  if (d.kind === "kpi") {
                    if (!isAccountVisible(d.anchorAccountId)) return null;
                    return (
                      <KpiTableRow
                        key={`kpi:${d.kpi.id}`}
                        kpi={d.kpi}
                        depth={d.anchorDepth}
                        buckets={visibleBuckets}
                      />
                    );
                  }
                  const r = d.row;
                  if (!isAccountVisible(r.account_id)) return null;
                  return (
                    <PivotTreeRow
                      key={`tree:${r.account_id}:${i}`}
                      row={r}
                      buckets={visibleBuckets}
                      cellAmount={cellAmount}
                      expanded={expanded.has(r.account_id)}
                      hasChildren={d.hasChildren}
                      onToggle={() => toggleExpand(r.account_id)}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        </>
      )}
    </div>
  );
}

// ----- Sub-render ------------------------------------------------------------

function KpiTableRow({
  kpi, depth, buckets,
}: {
  kpi: PivotKpi;
  depth: number;
  buckets: { key: string; granularity: string }[];
}) {
  // Cell-värde: summa över alla bolag i kpi.by_company för bucket
  function cellAmount(bucketKey: string): number | null {
    let total = 0;
    let any = false;
    for (const cid of Object.keys(kpi.by_company)) {
      const v = kpi.by_company[cid]?.[bucketKey];
      if (v !== null && v !== undefined) {
        total += v;
        any = true;
      }
    }
    return any ? total : null;
  }
  // För procent-KPI som täcker flera bolag: beräkna inte aritmetiskt-summa,
  // utan visa en tom cell (procent över aggregat ≠ summa procent).
  // Aritmetiskt medelvärde ger missvisande resultat — bättre att kräva enskild bolag.
  const isMultiCompany = Object.keys(kpi.by_company).length > 1;

  const isTotal    = kpi.emphasis === "total";
  const isMetric   = kpi.emphasis === "metric";
  const isSubtotal = kpi.emphasis === "subtotal";

  const labelCls =
    isTotal ? "font-semibold text-fg" :
    isMetric ? "italic text-fg-muted text-xs" :
    isSubtotal ? "font-medium text-fg-muted" : "text-fg-muted";

  const rowCls =
    isTotal ? "bg-elevated/50 border-y border-border" :
    isMetric ? "" :
    isSubtotal ? "bg-elevated/20" : "";

  const padLeft = `${depth * 14 + 12}px`;

  return (
    <tr className={`${rowCls}`}>
      <td className={`px-4 py-1.5 sticky left-0 bg-surface z-10 ${labelCls}`}>
        <div className="flex items-center gap-1" style={{ paddingLeft: padLeft }}>
          <span className="w-4 flex-shrink-0" />
          <span className="truncate">{kpi.label_sv}</span>
        </div>
      </td>
      {buckets.map((b) => {
        const raw = cellAmount(b.key);
        let display: string;
        if (raw === null) {
          display = "—";
        } else if (kpi.format === "percent") {
          // Procent får inte summeras över bolag → visa "—" om multi-bolag
          display = isMultiCompany ? "—" : fmtPercent(raw);
        } else {
          display = fmtCurrency(raw);
        }
        const isNeg = kpi.format === "currency" && raw !== null && raw < 0;
        return (
          <td
            key={b.key}
            className={`text-right px-3 py-1.5 tabular-nums whitespace-nowrap ${
              isTotal ? "font-semibold" : isMetric ? "italic text-fg-muted" : ""
            } ${bucketAccentClass(b, "cell")} ${isNeg ? "text-negative" : ""}`}
          >
            {display}
          </td>
        );
      })}
    </tr>
  );
}

function PivotTreeRow({
  row, buckets, cellAmount, expanded, hasChildren, onToggle,
}: {
  row: PivotRow;
  buckets: { key: string; granularity: string }[];
  cellAmount: (row: PivotRow, bucketKey: string) => number | null;
  expanded: boolean;
  hasChildren: boolean;
  onToggle: () => void;
}) {
  const isStorgrupp = row.is_aggregated && row.depth === 1;
  const isGrupp     = row.is_aggregated && row.depth === 2;
  const isLeaf      = !row.is_aggregated;

  const labelCls =
    isStorgrupp ? "font-semibold text-fg" :
    isGrupp     ? "font-medium text-fg" :
                  isLeaf ? "text-fg-muted" : "text-fg";

  const rowCls =
    isStorgrupp ? "border-t border-border bg-elevated/30" :
    isGrupp     ? "" :
                  "";

  const padLeft = `${row.depth * 14 + 12}px`;

  return (
    <tr className={`border-b border-border/30 hover:bg-elevated/50 ${rowCls}`}>
      <td className={`px-4 py-1.5 sticky left-0 bg-surface z-10 ${labelCls}`}>
        <div className="flex items-center gap-1" style={{ paddingLeft: padLeft }}>
          {hasChildren ? (
            <button
              onClick={onToggle}
              className="text-fg-muted hover:text-accent w-4 flex-shrink-0"
              aria-label={expanded ? "Fäll ihop" : "Expandera"}
            >
              {expanded
                ? <ChevronDown size={12} aria-hidden />
                : <ChevronRight size={12} aria-hidden />}
            </button>
          ) : (
            <span className="w-4 flex-shrink-0" />
          )}
          <span className="truncate" title={row.label_sv ?? undefined}>
            {row.account_code && (
              <span className="text-fg-muted/80 mr-1 tabular-nums">{row.account_code}</span>
            )}
            {row.label_sv || row.account_id}
          </span>
        </div>
      </td>
      {buckets.map((b) => {
        const raw = cellAmount(row, b.key);
        // Sign-flip för P&L-presentation (SIE-konvention → "intäkt positiv, kostnad negativ")
        const v = raw === null ? null : -raw;
        const isNeg = v !== null && v < 0;
        return (
          <td
            key={b.key}
            className={`text-right px-3 py-1.5 tabular-nums whitespace-nowrap ${
              isStorgrupp ? "font-semibold" : isGrupp ? "font-medium" : ""
            } ${bucketAccentClass(b, "cell")} ${isNeg ? "text-negative" : ""}`}
          >
            {fmtCurrency(v)}
          </td>
        );
      })}
    </tr>
  );
}
