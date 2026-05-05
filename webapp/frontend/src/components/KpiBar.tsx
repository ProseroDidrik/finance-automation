import { Kpi } from "../api";
import { fmtCurrency, fmtPercent } from "../lib/format";

interface Props {
  kpis: Kpi[];
  currency: string;
}

// De fyra KPI:erna som ska synas i top-baren.
const FEATURED = ["ebitda_adj", "ebit", "local_profit", "gross_margin"] as const;

const LABELS: Record<string, string> = {
  ebitda_adj:   "Justerad EBITDA",
  ebit:         "EBIT",
  local_profit: "Local profit",
  gross_margin: "Bruttomarginal",
};

export function KpiBar({ kpis, currency }: Props) {
  const byId = Object.fromEntries(kpis.map((k) => [k.id, k]));

  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {FEATURED.map((id) => {
        const k = byId[id];
        if (!k) return null;
        const isPct = k.format === "percent";
        const m = k.amount_month;
        const y = k.amount_ytd;
        const valueClass = (m ?? 0) >= 0 ? "text-fg" : "text-negative";
        return (
          <div
            key={id}
            className="bg-surface border border-border rounded-lg px-4 py-3"
          >
            <div className="text-2xs uppercase tracking-wider text-fg-muted font-medium">
              {LABELS[id]}
            </div>
            <div className={`mt-1 text-2xl font-semibold tabular ${valueClass}`}>
              {isPct ? fmtPercent(m) : fmtCurrency(m)}
              {!isPct && (
                <span className="ml-1 text-xs text-fg-muted font-normal">
                  k {currency}
                </span>
              )}
            </div>
            <div className="mt-0.5 text-xs text-fg-muted tabular">
              YTD: {isPct ? fmtPercent(y) : fmtCurrency(y)}
            </div>
          </div>
        );
      })}
    </div>
  );
}
