// API-klient mot FastAPI-backenden.

export interface Company {
  company_id: number;
  name: string;
  country: string;
  currency: string;
  n_periods: number;
  latest_period: string | null;
}

export interface Period {
  period: string;
  n_companies?: number;
}

export interface PnlRow {
  account_id: string;
  parent_id: string | null;
  label_sv: string;
  label_en: string;
  is_aggregated: boolean;
  depth: number;
  account_code: string | null;
  leaf_label: string | null;
  amount_month: number | null;
  amount_ytd: number | null;
  sort_path: string;
}

export interface Kpi {
  id: string;
  label_sv: string;
  label_en: string;
  anchor: string;
  format: "currency" | "percent";
  emphasis: "subtotal" | "total" | "metric";
  amount_month: number | null;
  amount_ytd: number | null;
}

export interface PnlReport {
  company: { company_id: number; name: string; country: string; currency: string };
  period: string;
  prev_period: string;
  year_start: string;
  rows: PnlRow[];
  kpis: Kpi[];
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

export async function fetchCompanies(): Promise<Company[]> {
  const d = await getJSON<{ companies: Company[] }>("/api/companies");
  return d.companies;
}

export async function fetchPeriods(companyId?: number): Promise<Period[]> {
  const url = companyId !== undefined
    ? `/api/periods?company_id=${companyId}`
    : "/api/periods";
  const d = await getJSON<{ periods: Period[] }>(url);
  return d.periods;
}

export async function fetchPnlReport(companyId: number, period: string): Promise<PnlReport> {
  return getJSON<PnlReport>(`/api/report/pnl?company_id=${companyId}&period=${period}`);
}

export interface CoverageRow {
  company_id: number;
  company_name: string;
  country: string;
  period: string;
  source_kind: string;
  scenario: string;
  backup_rows: number | null;
  fact_rows: number | null;
  backup_sum: number | null;
  fact_sum: number | null;
  status: "missing" | "mismatch" | "ok";
}

export async function fetchCoverage(): Promise<CoverageRow[]> {
  return getJSON<CoverageRow[]>("/api/compare/coverage");
}
