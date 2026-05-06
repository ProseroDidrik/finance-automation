import { useEffect, useMemo, useState } from "react";
import {
  ChevronDown, ChevronUp, ChevronsUpDown, TrendingDown, TrendingUp, X,
} from "lucide-react";
import {
  SupplierMeta, SupplierPivot, SupplierPivotRow,
  fetchSupplierMeta, fetchSuppliersByCategory, fetchSuppliersBySupplier,
} from "../api";
import { fmtCurrency, fmtGrowth, fmtPercent } from "../lib/format";

type SortKey = "name" | "total" | "growth" | "share";
type SortDir = "asc" | "desc";

const COUNTRY_LABEL: Record<string, string> = { Sweden: "Sverige" };

function SortIcon({ active, dir }: { active: boolean; dir: SortDir }) {
  if (!active) return <ChevronsUpDown size={12} className="text-fg-muted/50" aria-hidden />;
  return dir === "asc"
    ? <ChevronUp size={12} className="text-accent" aria-hidden />
    : <ChevronDown size={12} className="text-accent" aria-hidden />;
}

function GrowthCell({ v }: { v: number | null | undefined }) {
  if (v === null || v === undefined || Number.isNaN(v)) {
    return <span className="text-fg-muted">—</span>;
  }
  const Icon = v > 0 ? TrendingUp : v < 0 ? TrendingDown : null;
  const cls = v > 0 ? "text-positive" : v < 0 ? "text-negative" : "text-fg-muted";
  return (
    <span className={`inline-flex items-center gap-1 ${cls}`}>
      {Icon && <Icon size={12} aria-hidden />}
      {fmtGrowth(v)}
    </span>
  );
}

function sortRows(
  rows: SupplierPivotRow[], key: SortKey, dir: SortDir, latestYear: number,
  nameKey: "supplier_name" | "kategori",
): SupplierPivotRow[] {
  const out = [...rows];
  out.sort((a, b) => {
    let cmp = 0;
    if (key === "name") {
      cmp = ((a[nameKey] as string) ?? "").localeCompare((b[nameKey] as string) ?? "", "sv");
    } else if (key === "total") {
      cmp = (a.by_year[String(latestYear)] ?? 0) - (b.by_year[String(latestYear)] ?? 0);
    } else if (key === "growth") {
      cmp = (a.growth_yoy ?? -Infinity) - (b.growth_yoy ?? -Infinity);
    } else {
      cmp = (a.share_latest ?? 0) - (b.share_latest ?? 0);
    }
    return dir === "asc" ? cmp : -cmp;
  });
  return out;
}

interface PivotTableProps {
  title: string;
  pivot: SupplierPivot;
  nameKey: "supplier_name" | "kategori";
  showSegment?: boolean;
  search: string;
  topN: number;
}

function PivotTable({ title, pivot, nameKey, showSegment, search, topN }: PivotTableProps) {
  const [sortKey, setSortKey] = useState<SortKey>("total");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  const latestYear = pivot.compare_year ?? pivot.years[pivot.years.length - 1] ?? 0;

  function toggleSort(key: SortKey) {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir(key === "name" ? "asc" : "desc"); }
  }

  const filtered = useMemo(() => {
    let rows = pivot.rows;
    if (search.trim()) {
      const s = search.trim().toLowerCase();
      rows = rows.filter((r) => {
        const name = ((r[nameKey] as string) ?? "").toLowerCase();
        const seg = (r.segment ?? "").toLowerCase();
        return name.includes(s) || seg.includes(s);
      });
    }
    rows = sortRows(rows, sortKey, sortDir, latestYear, nameKey);
    if (topN > 0) rows = rows.slice(0, topN);
    return rows;
  }, [pivot.rows, search, sortKey, sortDir, latestYear, nameKey, topN]);

  const totals = useMemo(() => {
    const t: Record<string, number> = {};
    for (const y of pivot.years) t[String(y)] = 0;
    for (const r of pivot.rows) {
      for (const y of pivot.years) {
        const v = r.by_year[String(y)];
        if (v != null) t[String(y)] += v;
      }
    }
    return t;
  }, [pivot]);

  return (
    <section className="bg-surface border border-border rounded-md overflow-hidden">
      <header className="px-4 py-2.5 border-b border-border flex items-center gap-3">
        <h2 className="text-sm font-medium">{title}</h2>
        <span className="text-xs text-fg-muted">
          {filtered.length} av {pivot.rows.length} rader · belopp i tkr
        </span>
      </header>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead className="bg-elevated text-fg-muted">
            <tr>
              <th className="text-left px-3 py-2 font-medium">
                <button onClick={() => toggleSort("name")}
                        className="inline-flex items-center gap-1 hover:text-fg">
                  {nameKey === "supplier_name" ? "Leverantör" : "Kategori"}
                  <SortIcon active={sortKey === "name"} dir={sortDir} />
                </button>
              </th>
              {showSegment && (
                <th className="text-left px-3 py-2 font-medium">Segment</th>
              )}
              {pivot.years.map((y) => (
                <th key={y} className="text-right px-3 py-2 font-medium tabular-nums">
                  {y === latestYear ? (
                    <button onClick={() => toggleSort("total")}
                            className="inline-flex items-center gap-1 hover:text-fg">
                      {y}
                      <SortIcon active={sortKey === "total"} dir={sortDir} />
                    </button>
                  ) : y}
                </th>
              ))}
              <th className="text-right px-3 py-2 font-medium">
                <button onClick={() => toggleSort("growth")}
                        className="inline-flex items-center gap-1 hover:text-fg">
                  Tillväxt %
                  <SortIcon active={sortKey === "growth"} dir={sortDir} />
                </button>
              </th>
              <th className="text-right px-3 py-2 font-medium">
                <button onClick={() => toggleSort("share")}
                        className="inline-flex items-center gap-1 hover:text-fg">
                  Andel {latestYear}
                  <SortIcon active={sortKey === "share"} dir={sortDir} />
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {filtered.map((r, i) => (
              <tr key={i} className="border-t border-border hover:bg-elevated/40">
                <td className="px-3 py-1.5">{(r[nameKey] as string) ?? "—"}</td>
                {showSegment && (
                  <td className="px-3 py-1.5 text-fg-muted">{r.segment ?? "—"}</td>
                )}
                {pivot.years.map((y) => (
                  <td key={y} className="px-3 py-1.5 text-right tabular-nums">
                    {fmtCurrency(r.by_year[String(y)])}
                  </td>
                ))}
                <td className="px-3 py-1.5 text-right tabular-nums">
                  <GrowthCell v={r.growth_yoy} />
                </td>
                <td className="px-3 py-1.5 text-right tabular-nums">
                  {fmtPercent(r.share_latest)}
                </td>
              </tr>
            ))}
            {filtered.length === 0 && (
              <tr><td colSpan={pivot.years.length + (showSegment ? 4 : 3)}
                       className="px-3 py-4 text-center text-fg-muted">Inga rader matchar filtret.</td></tr>
            )}
          </tbody>
          <tfoot className="bg-elevated/40 font-medium">
            <tr className="border-t border-border">
              <td className="px-3 py-2">Totalt (urval före topp-N)</td>
              {showSegment && <td />}
              {pivot.years.map((y) => (
                <td key={y} className="px-3 py-2 text-right tabular-nums">
                  {fmtCurrency(totals[String(y)])}
                </td>
              ))}
              <td className="px-3 py-2 text-right text-fg-muted">—</td>
              <td className="px-3 py-2 text-right text-fg-muted">100%</td>
            </tr>
          </tfoot>
        </table>
      </div>
    </section>
  );
}

export function Suppliers() {
  const [country, setCountry] = useState<string>("Sweden");
  const [meta, setMeta] = useState<SupplierMeta | null>(null);
  const [bySupplier, setBySupplier] = useState<SupplierPivot | null>(null);
  const [byCategory, setByCategory] = useState<SupplierPivot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Filter
  const [companyIds, setCompanyIds] = useState<number[]>([]);
  const [segments, setSegments] = useState<string[]>([]);
  const [search, setSearch] = useState("");
  const [topN, setTopN] = useState<number>(50);
  const [compareYear, setCompareYear] = useState<number | null>(null);

  useEffect(() => {
    setLoading(true);
    setError(null);
    fetchSupplierMeta(country)
      .then((m) => {
        setMeta(m);
        // Default referensår: senaste FULL-året men hellre näst-senaste om
        // senaste år har H1-känsla (t.ex. 2025 är delvis). Heuristik: använd
        // år (max - 1) om max är innevarande år, annars max.
        const now = new Date().getFullYear();
        const fallback = m.years.includes(now - 1) && m.years.includes(now) && now === Math.max(...m.years)
          ? now - 1 : (m.years[m.years.length - 1] ?? null);
        setCompareYear(fallback);
      })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [country]);

  useEffect(() => {
    if (!meta || compareYear === null) return;
    setLoading(true);
    setError(null);
    const opts = { companyIds, segments, compareYear };
    Promise.all([
      fetchSuppliersBySupplier(country, opts),
      fetchSuppliersByCategory(country, opts),
    ])
      .then(([s, c]) => { setBySupplier(s); setByCategory(c); })
      .catch((e) => setError(String(e)))
      .finally(() => setLoading(false));
  }, [country, companyIds, segments, compareYear, meta]);

  function toggleCompany(id: number) {
    setCompanyIds((prev) =>
      prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]
    );
  }
  function toggleSegment(s: string) {
    setSegments((prev) =>
      prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]
    );
  }
  function clearFilters() { setCompanyIds([]); setSegments([]); setSearch(""); }

  return (
    <div className="space-y-5">
      {/* Filterrad */}
      <section className="bg-surface border border-border rounded-md p-3 space-y-3">
        <div className="flex items-center gap-3 flex-wrap">
          <div className="flex items-center gap-2">
            <label className="text-xs text-fg-muted">Land:</label>
            <select
              value={country}
              onChange={(e) => setCountry(e.target.value)}
              className="bg-bg border border-border rounded-md px-2 py-1 text-sm"
            >
              <option value="Sweden">{COUNTRY_LABEL.Sweden}</option>
            </select>
          </div>

          <div className="flex items-center gap-2">
            <label className="text-xs text-fg-muted">Sök:</label>
            <input
              type="text" value={search} onChange={(e) => setSearch(e.target.value)}
              placeholder="leverantör, kategori..."
              className="bg-bg border border-border rounded-md px-2 py-1 text-sm w-48"
            />
          </div>

          <div className="flex items-center gap-2">
            <label className="text-xs text-fg-muted">Topp-N:</label>
            <select
              value={topN} onChange={(e) => setTopN(parseInt(e.target.value))}
              className="bg-bg border border-border rounded-md px-2 py-1 text-sm"
            >
              <option value={25}>25</option>
              <option value={50}>50</option>
              <option value={100}>100</option>
              <option value={250}>250</option>
              <option value={0}>Alla</option>
            </select>
          </div>

          {meta && (
            <div className="flex items-center gap-2">
              <label className="text-xs text-fg-muted">Referensår:</label>
              <select
                value={compareYear ?? ""}
                onChange={(e) => setCompareYear(parseInt(e.target.value))}
                className="bg-bg border border-border rounded-md px-2 py-1 text-sm"
              >
                {meta.years.map((y) => (
                  <option key={y} value={y}>{y}</option>
                ))}
              </select>
            </div>
          )}

          {(companyIds.length > 0 || segments.length > 0 || search) && (
            <button onClick={clearFilters}
                    className="ml-auto inline-flex items-center gap-1 text-xs text-fg-muted hover:text-fg">
              <X size={12} /> Rensa filter
            </button>
          )}
        </div>

        {meta && (
          <>
            <div className="flex items-start gap-2 text-xs">
              <span className="text-fg-muted shrink-0 mt-1">Segment:</span>
              <div className="flex flex-wrap gap-1">
                {meta.segments.map((s) => (
                  <button
                    key={s}
                    onClick={() => toggleSegment(s)}
                    className={`px-2 py-0.5 rounded-md border ${
                      segments.includes(s)
                        ? "bg-accent text-white border-accent"
                        : "border-border text-fg-muted hover:text-fg"
                    }`}
                  >
                    {s}
                  </button>
                ))}
              </div>
            </div>

            <div className="flex items-start gap-2 text-xs">
              <span className="text-fg-muted shrink-0 mt-1">Bolag:</span>
              <div className="flex flex-wrap gap-1">
                {meta.companies.map((c) => {
                  const id = c.company_id ?? -1;
                  const active = companyIds.includes(id);
                  return (
                    <button
                      key={`${id}-${c.bolag_label}`}
                      onClick={() => toggleCompany(id)}
                      className={`px-2 py-0.5 rounded-md border ${
                        active
                          ? "bg-accent text-white border-accent"
                          : "border-border text-fg-muted hover:text-fg"
                      }`}
                      title={c.bolag_label ?? undefined}
                    >
                      {c.name ?? c.bolag_label ?? "?"}
                    </button>
                  );
                })}
              </div>
            </div>
          </>
        )}
      </section>

      {error && (
        <div className="bg-negative/15 text-negative border border-negative/30 rounded-md p-3 text-sm">
          {error}
        </div>
      )}

      {loading && !bySupplier && (
        <div className="text-fg-muted text-sm">Laddar leverantörsdata…</div>
      )}

      {bySupplier && (
        <PivotTable
          title="Per leverantör"
          pivot={bySupplier}
          nameKey="supplier_name"
          search={search}
          topN={topN}
        />
      )}

      {byCategory && (
        <PivotTable
          title="Per kategori"
          pivot={byCategory}
          nameKey="kategori"
          showSegment
          search={search}
          topN={topN}
        />
      )}
    </div>
  );
}
