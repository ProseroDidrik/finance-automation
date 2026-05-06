import { useMemo, useState } from "react";
import { ChevronRight, ChevronDown } from "lucide-react";
import { Kpi, PnlRow, PnlReport as PnlReportData } from "../api";
import { fmtCurrency, fmtPercent } from "../lib/format";

interface Props {
  data: PnlReportData;
}

// Display-rad: en av {tree-nod, kpi-rad}
type DisplayRow =
  | {
      kind: "tree";
      key: string;
      row: PnlRow;
      hasChildren: boolean;
      // visa-rader filtreras i render baserat på expanded state
    }
  | {
      kind: "kpi";
      key: string;
      kpi: Kpi;
      // KPI:er placeras under sin anchor (storgrupp/grupp). Visa när anchor är expanderad.
      parentAccountId: string;
      depth: number; // för indent
    };

/** Bygg display-listan: rows i sort_path-ordning + KPI:er infogade efter sin anchor. */
function buildDisplay(rows: PnlRow[], kpis: Kpi[]): DisplayRow[] {
  // Bygg parent-childrencount
  const childCount = new Map<string, number>();
  for (const r of rows) {
    if (r.parent_id) {
      childCount.set(r.parent_id, (childCount.get(r.parent_id) ?? 0) + 1);
    }
  }

  // Gruppera KPI:er per anchor (account_id)
  const kpiByAnchor = new Map<string, Kpi[]>();
  for (const k of kpis) {
    const arr = kpiByAnchor.get(k.anchor) ?? [];
    arr.push(k);
    kpiByAnchor.set(k.anchor, arr);
  }

  const out: DisplayRow[] = [];
  for (const r of rows) {
    out.push({
      kind: "tree",
      key: `tree:${r.sort_path}`,
      row: r,
      hasChildren: (childCount.get(r.account_id) ?? 0) > 0,
    });
  }

  // Andra passet: infoga KPI:er efter att subträdet är slut.
  // Walk through display-list och håll en stack av "open subtrees".
  // När vi möter en rad vars depth <= en open anchor's depth → flush KPI:er.
  return insertKpisAfterSubtree(out, kpiByAnchor);
}

function insertKpisAfterSubtree(
  display: DisplayRow[],
  kpiByAnchor: Map<string, Kpi[]>,
): DisplayRow[] {
  type OpenAnchor = { account_id: string; depth: number };
  const result: DisplayRow[] = [];
  const open: OpenAnchor[] = []; // anchors vars subträd vi befinner oss i

  function flushClosed(currentDepth: number) {
    // Stäng alla anchors där currentDepth <= anchor.depth (vi har lämnat subträdet)
    while (open.length > 0 && currentDepth <= open[open.length - 1].depth) {
      const a = open.pop()!;
      const ks = kpiByAnchor.get(a.account_id);
      if (ks) {
        for (const k of ks) {
          result.push({
            kind: "kpi",
            key: `kpi:${k.id}`,
            kpi: k,
            parentAccountId: a.account_id,
            depth: a.depth, // KPI ligger på samma indent som anchor
          });
        }
      }
    }
  }

  for (const d of display) {
    if (d.kind !== "tree") continue;
    // Innan vi pushar nya: stäng eventuella anchors vi nu lämnat
    flushClosed(d.row.depth);
    result.push(d);
    if (kpiByAnchor.has(d.row.account_id)) {
      open.push({ account_id: d.row.account_id, depth: d.row.depth });
    }
  }
  // Flush kvarvarande
  flushClosed(0);
  return result;
}

// ---------- Komponent --------------------------------------------------------

export function PnlReport({ data }: Props) {
  // expanded: Set av account_ids som är öppna. Default: bara "P&L"-roten,
  // dvs. enbart storgrupperna (depth=1) syns. Användaren klickar för att
  // expandera djupare.
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(["P&L"]));

  const display = useMemo(() => buildDisplay(data.rows, data.kpis), [data]);

  // Avgör om en rad är synlig: walka uppåt via parent_id och kolla att alla parents är expanded
  const visibleRow = (row: PnlRow) => {
    if (row.depth === 0) return true;
    let pid = row.parent_id;
    while (pid) {
      if (!expanded.has(pid)) return false;
      pid = parentOf(data.rows, pid);
    }
    return true;
  };

  function toggle(accountId: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(accountId)) next.delete(accountId);
      else next.add(accountId);
      return next;
    });
  }

  function expandAll() {
    const s = new Set<string>(["P&L"]);
    for (const r of data.rows) if (r.is_aggregated) s.add(r.account_id);
    setExpanded(s);
  }
  function collapseAll() {
    setExpanded(new Set(["P&L"]));
  }

  // Räkna out child-counts en gång för render
  const childCount = useMemo(() => {
    const c = new Map<string, number>();
    for (const r of data.rows) {
      if (r.parent_id) c.set(r.parent_id, (c.get(r.parent_id) ?? 0) + 1);
    }
    return c;
  }, [data]);

  return (
    <div className="border border-border rounded-lg overflow-hidden bg-surface">
      <div className="flex items-center justify-between border-b border-border px-4 py-2 text-2xs uppercase tracking-wider text-fg-muted">
        <span>Resultaträkning <span className="ml-1 text-fg-muted/80">— belopp i tusental</span></span>
        <div className="flex gap-3">
          <button onClick={expandAll}  className="hover:text-accent">Expand all</button>
          <button onClick={collapseAll} className="hover:text-accent">Collapse</button>
        </div>
      </div>

      <table className="w-full text-sm">
        <thead className="bg-elevated text-fg-muted text-2xs uppercase tracking-wider sticky top-0">
          <tr>
            <th className="text-left  px-4 py-2 font-medium w-2/3">Konto / grupp</th>
            <th className="text-right px-4 py-2 font-medium">Period</th>
            <th className="text-right px-4 py-2 font-medium">YTD</th>
            <th className="text-right px-4 py-2 font-medium">Budget YTD</th>
          </tr>
        </thead>
        <tbody>
          {display.map((d) => {
            if (d.kind === "kpi") {
              return (
                <KpiRow
                  key={d.key}
                  kpi={d.kpi}
                  depth={d.depth}
                  visible={parentVisible(d.parentAccountId, data.rows, expanded)}
                />
              );
            }
            const r = d.row;
            if (!visibleRow(r)) return null;
            return (
              <TreeRowComp
                key={d.key}
                row={r}
                expanded={expanded.has(r.account_id)}
                hasChildren={(childCount.get(r.account_id) ?? 0) > 0}
                onToggle={() => toggle(r.account_id)}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------- Sub-renderers ----------------------------------------------------

function TreeRowComp({
  row, expanded, hasChildren, onToggle,
}: {
  row: PnlRow;
  expanded: boolean;
  hasChildren: boolean;
  onToggle: () => void;
}) {
  // Sign-flip vid display
  const m = row.amount_month === null ? null : -row.amount_month;
  const y = row.amount_ytd === null ? null : -row.amount_ytd;
  const yb = row.amount_ytd_budget === null ? null : -row.amount_ytd_budget;

  const padLeft = `pl-[${row.depth * 16 + 16}px]`;

  // Stilval beroende på nod-typ
  const isLeaf = !row.is_aggregated; // bolagskonto
  const isStorgrupp = row.is_aggregated && row.depth === 1;
  const isGrupp = row.is_aggregated && row.depth === 2;

  let labelClass = "text-fg";
  let bgClass = "";
  if (isStorgrupp) labelClass = "text-fg font-medium";
  if (isGrupp) labelClass = "text-fg";
  if (isLeaf) labelClass = "text-fg-muted text-xs";
  // Grupp-rader (Mercurs gröna): bakgrund som accent-ish — använd elevated subtilt
  if (isGrupp) bgClass = "bg-elevated/40";

  const label = isLeaf
    ? `${row.account_code} ${row.leaf_label ?? row.label_sv}`
    : row.label_sv;

  return (
    <tr className={`border-t border-border hover:bg-elevated/60 ${bgClass}`}>
      <td className={`px-4 py-1.5 ${padLeft}`} style={{ paddingLeft: row.depth * 16 + 16 }}>
        <span className="inline-flex items-center gap-1">
          {hasChildren ? (
            <button
              onClick={onToggle}
              className="text-fg-muted hover:text-fg transition-colors duration-180"
              aria-label={expanded ? "Collapse" : "Expand"}
            >
              {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </button>
          ) : (
            <span className="w-[14px] inline-block" />
          )}
          <span className={labelClass}>{label}</span>
        </span>
      </td>
      <td className={`px-4 py-1.5 text-right tabular ${amountClass(m)}`}>{fmtCurrency(m)}</td>
      <td className={`px-4 py-1.5 text-right tabular ${amountClass(y)}`}>{fmtCurrency(y)}</td>
      <td className={`px-4 py-1.5 text-right tabular text-fg-muted ${amountClass(yb)}`}>{fmtCurrency(yb)}</td>
    </tr>
  );
}

function KpiRow({ kpi, depth, visible }: { kpi: Kpi; depth: number; visible: boolean }) {
  if (!visible) return null;
  const m = kpi.amount_month;
  const y = kpi.amount_ytd;
  const yb = kpi.amount_ytd_budget;
  const isPct = kpi.format === "percent";
  const isTotal = kpi.emphasis === "total";
  const isMetric = kpi.emphasis === "metric";

  const rowClass = isTotal
    ? "border-t-2 border-border bg-elevated/80 font-semibold"
    : isMetric
      ? "border-t border-border text-fg-muted italic text-xs"
      : "border-t border-border bg-elevated/30";

  return (
    <tr className={rowClass}>
      <td className="px-4 py-1.5" style={{ paddingLeft: depth * 16 + 30 }}>
        {kpi.label_sv}
      </td>
      <td className={`px-4 py-1.5 text-right tabular ${!isMetric ? amountClass(m) : ""}`}>
        {isPct ? fmtPercent(m) : fmtCurrency(m)}
      </td>
      <td className={`px-4 py-1.5 text-right tabular ${!isMetric ? amountClass(y) : ""}`}>
        {isPct ? fmtPercent(y) : fmtCurrency(y)}
      </td>
      <td className={`px-4 py-1.5 text-right tabular text-fg-muted ${!isMetric ? amountClass(yb) : ""}`}>
        {isPct ? fmtPercent(yb) : fmtCurrency(yb)}
      </td>
    </tr>
  );
}

// ---------- Helpers ----------------------------------------------------------

function parentOf(rows: PnlRow[], accountId: string): string | null {
  const r = rows.find((x) => x.account_id === accountId);
  return r?.parent_id ?? null;
}

function parentVisible(accountId: string, rows: PnlRow[], expanded: Set<string>): boolean {
  // Anchor är synlig om alla dess parents är expanderade
  let pid = parentOf(rows, accountId);
  while (pid) {
    if (!expanded.has(pid)) return false;
    pid = parentOf(rows, pid);
  }
  return true;
}

function amountClass(v: number | null): string {
  if (v === null || Number.isNaN(v)) return "text-fg-muted";
  if (v < 0) return "text-negative";
  return "text-fg";
}
