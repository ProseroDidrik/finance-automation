import { useEffect, useMemo, useRef, useState } from "react";
import { X, CheckCircle2, AlertTriangle, CircleSlash } from "lucide-react";
import {
  CoverageAccountRow,
  CoverageAccountsReport,
  fetchCoverageAccounts,
} from "../api";
import { fmtCurrency } from "../lib/format";

export interface CoverageAccountsSelection {
  company_id: number;
  company_name: string | null;
  period: string;
  source_kind: string;
}

type SortKey = "account_code" | "account_name" | "facit_amt" | "fact_amt" | "diff" | "status_acc";
type SortDir = "asc" | "desc";

const STATUS_SORT_ORDER: Record<CoverageAccountRow["status_acc"], number> = {
  amount_diff: 0,
  only_facit:  1,
  only_fact:   2,
  ok:          3,
};

const STATUS_LABEL: Record<CoverageAccountRow["status_acc"], string> = {
  amount_diff: "Belopp avviker",
  only_facit:  "Saknas i fact",
  only_fact:   "Extra i fact",
  ok:          "OK",
};

const STATUS_CLS: Record<CoverageAccountRow["status_acc"], string> = {
  amount_diff: "bg-warn/15 text-warn",
  only_facit:  "bg-negative/15 text-negative",
  only_fact:   "bg-warn/15 text-warn",
  ok:          "text-positive",
};

// Tooltip för status_acc — förklaras vid hover i drawer-tabellen.
const STATUS_TITLE: Record<CoverageAccountRow["status_acc"], string> = {
  amount_diff: "Båda källor har raden men beloppen skiljer sig",
  only_facit:  "Mercur-facit har raden men fact saknar den",
  only_fact:   "fact har raden men Mercur saknar den",
  ok:          "Belopp matchar inom tröskel",
};

interface Props {
  selection: CoverageAccountsSelection | null;
  onClose: () => void;
}

export function CoverageAccountsDrawer({ selection, onClose }: Props) {
  const [data, setData]       = useState<CoverageAccountsReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError]     = useState<string | null>(null);
  const [hideOk, setHideOk]   = useState(true);
  const [sortKey, setSortKey] = useState<SortKey>("status_acc");
  const [sortDir, setSortDir] = useState<SortDir>("asc");

  // Hämta data när selection ändras. `cancelled`-flagga skyddar mot race
  // när användaren klickar rad A → rad B snabbt och A:s svar anländer efter
  // B:s — annars skulle B:s data skrivas över med A:s.
  useEffect(() => {
    if (!selection) { setData(null); return; }
    let cancelled = false;
    setLoading(true); setError(null);
    fetchCoverageAccounts({
      company_id:  selection.company_id,
      period:      selection.period,
      source_kind: selection.source_kind,
    })
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e) => { if (!cancelled) setError(String(e)); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [selection?.company_id, selection?.period, selection?.source_kind]);

  // Escape-stäng + body scroll-lock + fokus-återlämning till klickad rad
  // (spec §5a). returnFocusRef pekar på elementet som hade fokus när drawern
  // öppnades — vid stäng flyttar vi fokus tillbaka dit.
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useEffect(() => {
    if (!selection) return;
    returnFocusRef.current = document.activeElement as HTMLElement | null;
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", onKey);
    const prev = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = prev;
      returnFocusRef.current?.focus?.();
    };
  }, [selection, onClose]);

  // Filtrering: "Visa bara avvikelser" döljer ok-rader.
  const sortedRows = useMemo(() => {
    if (!data) return [] as CoverageAccountRow[];
    const rows = hideOk
      ? data.rows.filter((r) => r.status_acc !== "ok")
      : data.rows;
    return [...rows].sort((a, b) => {
      let va: string | number, vb: string | number;
      switch (sortKey) {
        case "account_code": va = a.account_code; vb = b.account_code; break;
        case "account_name": va = a.account_name ?? ""; vb = b.account_name ?? ""; break;
        case "facit_amt":    va = a.facit_amt ?? 0; vb = b.facit_amt ?? 0; break;
        case "fact_amt":     va = a.fact_amt  ?? 0; vb = b.fact_amt  ?? 0; break;
        case "diff":         va = Math.abs(a.diff ?? 0); vb = Math.abs(b.diff ?? 0); break;
        case "status_acc":   va = STATUS_SORT_ORDER[a.status_acc]; vb = STATUS_SORT_ORDER[b.status_acc]; break;
      }
      const cmp = va < vb ? -1 : va > vb ? 1 : 0;
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [data, hideOk, sortKey, sortDir]);

  function toggleSort(k: SortKey) {
    if (sortKey === k) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else {
      setSortKey(k);
      // Belopp/diff sorteras default desc (störst först); namn/kod/status asc.
      setSortDir(k === "diff" || k === "facit_amt" || k === "fact_amt" ? "desc" : "asc");
    }
  }

  if (!selection) return null;

  return (
    <>
      {/* Overlay */}
      <div
        className="fixed inset-0 bg-black/40 z-40"
        onClick={onClose}
        aria-hidden
      />
      {/* Drawer */}
      <div
        role="dialog"
        aria-modal="true"
        aria-label={`Konto-diff för ${selection.company_name ?? selection.company_id}, period ${selection.period}, källa ${selection.source_kind}`}
        className="fixed right-0 top-0 bottom-0 w-[640px] max-w-[95vw] bg-bg border-l border-border shadow-xl z-50 flex flex-col"
      >
        {/* Header */}
        <div className="flex items-start justify-between px-4 py-3 border-b border-border">
          <div>
            <h2 className="text-sm font-semibold">
              {selection.company_name ?? `Bolag ${selection.company_id}`}
            </h2>
            <p className="text-xs text-fg-muted mt-0.5">
              Period {selection.period} · Källa <span className="font-mono">{selection.source_kind}</span>
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            autoFocus
            className="text-fg-muted hover:text-fg p-1 -m-1 cursor-pointer"
            aria-label="Stäng"
          >
            <X size={18} />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="text-fg-muted text-sm py-8 text-center">Hämtar konto-diff…</div>
          )}
          {error && (
            <div role="alert" className="m-3 bg-negative/10 border border-negative/30 text-negative text-sm rounded-md p-3">
              {error}
            </div>
          )}
          {data && !loading && !error && (
            <>
              {/* Summary chips */}
              <div className="flex flex-wrap items-center gap-4 text-xs px-4 py-3 border-b border-border/50">
                {data.summary.n_amount_diff > 0 && (
                  <span className="flex items-center gap-1.5 text-warn font-semibold">
                    <AlertTriangle size={12} aria-hidden /> {data.summary.n_amount_diff} belopp avviker
                  </span>
                )}
                {data.summary.n_only_facit > 0 && (
                  <span className="flex items-center gap-1.5 text-negative font-semibold">
                    <CircleSlash size={12} aria-hidden /> {data.summary.n_only_facit} saknas i fact
                  </span>
                )}
                {data.summary.n_only_fact > 0 && (
                  <span className="flex items-center gap-1.5 text-warn">
                    <AlertTriangle size={12} aria-hidden /> {data.summary.n_only_fact} extra i fact
                  </span>
                )}
                {data.summary.n_ok > 0 && (
                  <span className="flex items-center gap-1.5 text-positive">
                    <CheckCircle2 size={12} aria-hidden /> {data.summary.n_ok} ok
                  </span>
                )}
                <span className="ml-auto text-fg-muted tabular-nums">
                  Σ facit {fmtCurrency(data.summary.facit_sum)} · fact {fmtCurrency(data.summary.fact_sum)}
                </span>
              </div>

              {/* Filter toggle */}
              <div className="px-4 py-2 border-b border-border/50">
                <label className="text-xs text-fg-muted inline-flex items-center gap-1.5 cursor-pointer">
                  <input
                    type="checkbox"
                    checked={hideOk}
                    onChange={(e) => setHideOk(e.target.checked)}
                  />
                  Visa bara avvikelser
                </label>
              </div>

              {/* Table */}
              {sortedRows.length === 0 ? (
                <div className="text-center text-fg-muted text-sm py-12">
                  {hideOk
                    ? `Inga avvikelser — ${data.summary.n_ok} konton stämmer ✓`
                    : "Inga rader"}
                </div>
              ) : (
                <table className="w-full text-xs">
                  <thead className="sticky top-0 bg-surface border-b border-border">
                    <tr>
                      <th
                        onClick={() => toggleSort("account_code")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Konto</th>
                      <th
                        onClick={() => toggleSort("account_name")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Namn</th>
                      <th
                        onClick={() => toggleSort("facit_amt")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Facit</th>
                      <th
                        onClick={() => toggleSort("fact_amt")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Fact</th>
                      <th
                        onClick={() => toggleSort("diff")}
                        className="px-3 py-2 text-right font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Diff</th>
                      <th
                        onClick={() => toggleSort("status_acc")}
                        className="px-3 py-2 text-left font-medium text-fg-muted cursor-pointer hover:bg-elevated"
                      >Status</th>
                    </tr>
                  </thead>
                  <tbody>
                    {sortedRows.map((r) => (
                      <tr key={r.account_code} className="border-b border-border/50">
                        <td className="px-3 py-1.5 font-mono whitespace-nowrap">{r.account_code}</td>
                        <td className="px-3 py-1.5">{r.account_name ?? "—"}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.facit_amt)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.fact_amt)}</td>
                        <td className="px-3 py-1.5 text-right tabular-nums">{fmtCurrency(r.diff)}</td>
                        <td className="px-3 py-1.5">
                          <span
                            title={STATUS_TITLE[r.status_acc]}
                            className={`inline-block px-1.5 py-0.5 rounded text-xs font-medium ${STATUS_CLS[r.status_acc]}`}
                          >
                            {STATUS_LABEL[r.status_acc]}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </>
          )}
        </div>
      </div>
    </>
  );
}
