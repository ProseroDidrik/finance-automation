// Tal- och period-formatering.
// Belopp visas i tusental (k) — dvs ÷1000, avrundat till heltal.

const _fmt0 = new Intl.NumberFormat("sv-SE", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});
const _fmtPct = new Intl.NumberFormat("sv-SE", {
  style: "percent",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const MONTH_SV = [
  "Januari", "Februari", "Mars", "April", "Maj", "Juni",
  "Juli", "Augusti", "September", "Oktober", "November", "December",
];

/** Returnerar belopp i tusental (k), avrundat till heltal.
 *  6,061,618.76 → "6 062" */
export function fmtCurrency(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return _fmt0.format(v / 1000);
}

export function fmtPercent(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return _fmtPct.format(v);
}

export function fmtPeriod(p: string): string {
  if (!/^\d{6}$/.test(p)) return p;
  const y = p.slice(0, 4);
  const m = parseInt(p.slice(4), 10);
  return `${MONTH_SV[m - 1]} ${y}`;
}

const MONTH_SV_SHORT = MONTH_SV.map((m) => m.slice(0, 3));

/** Formatera en bucket-key från pivot-rapporten. Stödjer: YYYYMM,
 *  YYYY-Q[1-4], YYYY-H[1-2], YYYY, LTM. */
export function fmtBucket(key: string): string {
  if (key === "LTM") return "LTM";
  if (/^\d{4}-Q[1-4]$/.test(key)) return `Q${key[6]} ${key.slice(0, 4)}`;
  if (/^\d{4}-H[12]$/.test(key)) return `H${key[6]} ${key.slice(0, 4)}`;
  if (/^\d{4}$/.test(key)) return key;
  if (/^\d{6}$/.test(key)) {
    const y = key.slice(0, 4);
    const m = parseInt(key.slice(4), 10);
    return `${MONTH_SV_SHORT[m - 1]} ${y}`;
  }
  return key;
}

export function fmtGrowth(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const sign = v > 0 ? "+" : "";
  return sign + _fmtPct.format(v);
}
