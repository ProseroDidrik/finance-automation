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
  amount_ytd_budget: number | null;
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
  amount_ytd_budget: number | null;
}

export interface PnlReport {
  company: { company_id: number; name: string; country: string; currency: string };
  period: string;
  prev_period: string;
  year_start: string;
  rows: PnlRow[];
  kpis: Kpi[];
}

// Easy Auth signalerar utgången session med 401. Tjänsten har en inbyggd
// /.auth/login/aad-endpoint som triggar Entra ID-redirect och därefter
// återgår till post_login_redirect_uri. Vi skickar med location.href så
// användaren landar tillbaka på samma sida efter inloggning.
function redirectToLogin(): void {
  const here = window.location.pathname + window.location.search;
  const target = `/.auth/login/aad?post_login_redirect_uri=${encodeURIComponent(here)}`;
  window.location.assign(target);
}

async function checkAuth(res: Response): Promise<void> {
  // 401 = sessionen utgången / Easy Auth har släppt cookien. Skicka tillbaka
  // till login. 403 = inloggad men saknar Maestro-grupp — visa felet i UI:t.
  if (res.status === 401) {
    redirectToLogin();
    // Throwa så anropare ser fel medan redirect:en sker (location.assign är
    // asynkron i praktiken).
    throw new Error("Sessionen gick ut — omdirigerar till login.");
  }
  if (!res.ok) {
    throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  }
}

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  await checkAuth(res);
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

export async function fetchPnlReport(
  companyId: number,
  period: string,
  sourceKind?: string | null,
): Promise<PnlReport> {
  const params = new URLSearchParams({
    company_id: String(companyId),
    period,
  });
  if (sourceKind) params.append("source_kind", sourceKind);
  return getJSON<PnlReport>(`/api/report/pnl?${params}`);
}

export interface ReportOptions {
  sources: { value: string; n_rows: number }[];
}

export async function fetchReportOptions(
  companyId: number,
  period: string,
): Promise<ReportOptions> {
  return getJSON<ReportOptions>(
    `/api/report/options?company_id=${companyId}&period=${period}`,
  );
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
  status: "missing" | "missing_zero" | "mismatch" | "ok" | "extra";
}

export async function fetchCoverage(opts: { periodFrom?: string; periodTo?: string } = {}): Promise<CoverageRow[]> {
  const p = new URLSearchParams();
  if (opts.periodFrom) p.append("period_from", opts.periodFrom);
  if (opts.periodTo) p.append("period_to", opts.periodTo);
  const qs = p.toString();
  return getJSON<CoverageRow[]>(`/api/compare/coverage${qs ? `?${qs}` : ""}`);
}

export interface CoverageAccountRow {
  account_code: string;
  account_name: string | null;
  facit_amt: number | null;
  fact_amt: number | null;
  diff: number | null;
  status_acc: "ok" | "amount_diff" | "only_facit" | "only_fact";
}

export interface CoverageAccountsSummary {
  n_ok: number;
  n_amount_diff: number;
  n_only_facit: number;
  n_only_fact: number;
  facit_sum: number;
  fact_sum: number;
}

export interface CoverageAccountsReport {
  company_id: number;
  company_name: string | null;
  period: string;
  source_kind: string;
  rows: CoverageAccountRow[];
  summary: CoverageAccountsSummary;
}

export async function fetchCoverageAccounts(opts: {
  company_id: number;
  period: string;
  source_kind: string;
}): Promise<CoverageAccountsReport> {
  const p = new URLSearchParams({
    company_id:  String(opts.company_id),
    period:      opts.period,
    source_kind: opts.source_kind,
  });
  return getJSON<CoverageAccountsReport>(`/api/compare/coverage/accounts?${p}`);
}

// ----- Personnel (FTE) -------------------------------------------------------

export interface PersonnelCountry {
  country: string;
  n_rows: number;
  n_companies: number;
  snapshot_date: string | null;
}

export interface PersonnelYear {
  ub: number;
  began: number;
  slutat: number;
}

export interface PersonnelCompanyRow {
  company_id: number;
  company_name: string;
  years: Record<string, PersonnelYear>;
}

export interface PersonnelSummary {
  country: string;
  years: number[];
  rows: PersonnelCompanyRow[];
}

export interface PersonnelEmployee {
  employee_name: string;
  title: string | null;
  birth_date: string | null;
  employed_from: string | null;
  employed_to: string | null;
  termination_reason: string | null;
  employment_pct: number | null;
  productivity: number | null;
  billable_pct: number | null;
  gender: string | null;
  category: string | null;
  salary_local: number | null;
  location: string | null;
  apprenticeship_end: string | null;
  pension_apprentice: string | null;
}

export interface PersonnelEmployees {
  company: { company_id: number; name: string; country: string; currency: string };
  employees: PersonnelEmployee[];
}

export async function fetchPersonnelCountries(): Promise<PersonnelCountry[]> {
  const d = await getJSON<{ countries: PersonnelCountry[] }>("/api/personnel/countries");
  return d.countries;
}

export async function fetchPersonnelSummary(country: string): Promise<PersonnelSummary> {
  return getJSON<PersonnelSummary>(`/api/personnel/summary?country=${encodeURIComponent(country)}`);
}

export async function fetchPersonnelEmployees(companyId: number): Promise<PersonnelEmployees> {
  return getJSON<PersonnelEmployees>(`/api/personnel/employees?company_id=${companyId}`);
}

// ----- Pivot ----------------------------------------------------------------

export type Granularity = "month" | "quarter" | "half" | "year";
export type ReportCurrency = "SEK" | "LOCAL";

export interface PivotBucket {
  key: string;
  label: string;
  start: string;
  end: string;
  granularity: Granularity | "ltm";
}

export interface PivotCompany {
  company_id: number;
  name: string | null;
  country: string | null;
  currency: string | null;
  kind: string | null;
  parent_id: number | null;
  acquisition_year: number | null;
}

export interface PivotRow {
  account_id: string;
  parent_id: string | null;
  label_sv: string | null;
  label_en: string | null;
  is_aggregated: boolean;
  depth: number;
  account_code: string | null;
  leaf_label: string | null;
  sort_path: string | null;
  by_company: Record<string, Record<string, number | null>>; // company_id → bucket_key → amount
}

export interface PivotKpi {
  id: string;
  label_sv: string;
  label_en: string;
  anchor: string;
  format: "currency" | "percent";
  emphasis: "subtotal" | "total" | "metric";
  by_company: Record<string, Record<string, number | null>>;
}

export interface PivotReport {
  buckets: PivotBucket[];
  companies: PivotCompany[];
  rows: PivotRow[];
  kpis: PivotKpi[];
  report_currency: ReportCurrency;
  scenario: "A" | "B";
  granularity: Granularity;
  period_from: string;
  period_to: string;
}

export interface PivotQuery {
  country?: string;
  company_ids?: number[];
  period_from: string;
  period_to: string;
  granularity: Granularity;
  report_currency: ReportCurrency;
  include_ltm: boolean;
  include_ytd?: boolean;
  scenario?: "A" | "B";
  source_kind?: string;
}

// ----- Counterparties -------------------------------------------------------

export interface CounterpartyPeriod {
  period: string;
  has_csv: boolean;
  has_saft: boolean;
  n_saft_files: number;
}

export interface CounterpartyCompany {
  company_id: number | null;
  company_label: string;
  source_file: string;
}

export interface CounterpartyRow {
  orgnr: string;
  type: string;
  country: string | null;
  name_saft: string | null;
  name_brreg: string | null;
  brreg_found: string;
  konkurs: boolean;
  under_avvikling: boolean;
  tvangsavvikling: boolean;
  sanctions_review: string | null;
  status: "ok" | "flagged";
  badges: string[];
  companies: CounterpartyCompany[];
  source_file: string | null;
}

export interface CounterpartyReport {
  period: string;
  csv_exists: boolean;
  rows: CounterpartyRow[];
  n_total?: number;
  n_flagged?: number;
  message?: string;
}

export interface CounterpartyRunStatus {
  running: boolean;
  run_id: string | null;
  period: string | null;
  with_sanctions: boolean;
  include_customers: boolean;
  started_at: string | null;
  completed_at: string | null;
  log_tail: string[];
  return_code: number | null;
  error: string | null;
}

export async function fetchCounterpartyPeriods(): Promise<CounterpartyPeriod[]> {
  const d = await getJSON<{ periods: CounterpartyPeriod[] }>("/api/counterparties/periods");
  return d.periods;
}

export async function fetchCounterparties(period: string): Promise<CounterpartyReport> {
  return getJSON<CounterpartyReport>(`/api/counterparties?period=${period}`);
}

export async function fetchCounterpartyRunStatus(): Promise<CounterpartyRunStatus> {
  return getJSON<CounterpartyRunStatus>("/api/counterparties/run/status");
}

export async function startCounterpartyRun(
  period: string, withSanctions: boolean, includeCustomers: boolean,
): Promise<CounterpartyRunStatus> {
  const res = await fetch("/api/counterparties/run", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      period,
      with_sanctions: withSanctions,
      include_customers: includeCustomers,
    }),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}: ${await res.text()}`);
  return res.json();
}

// ----- Suppliers ------------------------------------------------------------

export interface SupplierCompany {
  company_id: number | null;
  name: string | null;
  bolag_label: string | null;
  latest_total: number | null;
}

export interface SupplierMeta {
  country: string;
  n_rows: number;
  years: number[];
  segments: string[];
  kategorier: string[];
  companies: SupplierCompany[];
}

export interface SupplierPivotRow {
  supplier_name?: string | null;
  kategori?: string | null;
  segment?: string | null;
  by_year: Record<string, number | null>;
  total_latest: number | null;
  growth_yoy: number | null;
  share_latest: number | null;
}

export interface SupplierPivot {
  country: string;
  years: number[];
  compare_year: number | null;
  rows: SupplierPivotRow[];
}

export async function fetchSupplierMeta(country: string): Promise<SupplierMeta> {
  return getJSON<SupplierMeta>(`/api/suppliers/meta?country=${encodeURIComponent(country)}`);
}

interface SupplierFetchOpts {
  companyIds?: number[];
  segments?: string[];
  includeUncategorized?: boolean;
  compareYear?: number;
}

function buildSupplierParams(country: string, opts: SupplierFetchOpts): URLSearchParams {
  const p = new URLSearchParams({ country });
  if (opts.companyIds?.length) p.append("company_ids", opts.companyIds.join(","));
  if (opts.segments?.length) p.append("segments", opts.segments.join(","));
  if (opts.includeUncategorized !== undefined)
    p.append("include_uncategorized", String(opts.includeUncategorized));
  if (opts.compareYear !== undefined) p.append("compare_year", String(opts.compareYear));
  return p;
}

export async function fetchSuppliersBySupplier(
  country: string, opts: SupplierFetchOpts = {},
): Promise<SupplierPivot> {
  return getJSON<SupplierPivot>(`/api/suppliers/by_supplier?${buildSupplierParams(country, opts)}`);
}

export async function fetchSuppliersByCategory(
  country: string, opts: SupplierFetchOpts = {},
): Promise<SupplierPivot> {
  return getJSON<SupplierPivot>(`/api/suppliers/by_category?${buildSupplierParams(country, opts)}`);
}

export async function fetchPivot(q: PivotQuery): Promise<PivotReport> {
  const params = new URLSearchParams();
  if (q.country) params.append("country", q.country);
  if (q.company_ids && q.company_ids.length)
    params.append("company_ids", q.company_ids.join(","));
  params.append("period_from", q.period_from);
  params.append("period_to", q.period_to);
  params.append("granularity", q.granularity);
  params.append("report_currency", q.report_currency);
  params.append("include_ltm", String(q.include_ltm));
  if (q.include_ytd) params.append("include_ytd", "true");
  if (q.scenario) params.append("scenario", q.scenario);
  if (q.source_kind) params.append("source_kind", q.source_kind);
  return getJSON<PivotReport>(`/api/report/pivot?${params}`);
}
