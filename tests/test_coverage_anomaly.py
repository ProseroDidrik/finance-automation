"""bug_003-regression: clobber-detektorns baslinje måste vara MAX, inte median.
Median räknas över samma månader vi letar anomalier i → när >hälften av en
högvolym-FY är clobbrad kollapsar medianen under tröskeln och hela FY:t hoppas
över (de VÄRST clobbrade FY:n gav noll träffar). MAX är robust.
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import check_journal_coverage_anomaly as guard


def _fy(cid, year, counts):
    return [(cid, f"{year}{m:02d}", n) for m, n in enumerate(counts, start=1)]


class FlagAnomalies(unittest.TestCase):
    def test_majority_clobbered_fy_still_flagged(self):
        # 7 av 12 månader clobbrade till 8, 5 intakta vid 3000 (median=8 → gamla
        # koden hoppade hela FY:t; max=3000 → tröskel 300, alla 7 flaggas).
        rows = _fy(9, "2023", [8, 8, 8, 8, 8, 8, 8, 3000, 3000, 3000, 3000, 3000])
        flagged = guard.flag_anomalies(rows)
        self.assertEqual(len(flagged), 7)
        self.assertTrue(all(n == 8 for _cid, _p, n, _base in flagged))
        self.assertTrue(all(base == 3000 for *_x, base in flagged))

    def test_minority_clobbered_fy_flagged(self):
        rows = _fy(9, "2024", [8, 8, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000, 3000])
        flagged = guard.flag_anomalies(rows)
        self.assertEqual(len(flagged), 2)

    def test_low_volume_fy_not_flagged(self):
        # Genuint lågvolym-bolag (max < MIN_BASELINE) → ingen flagga.
        rows = _fy(52, "2025", [5, 8, 3, 6, 4, 7, 2, 9, 5, 8, 3, 6])
        self.assertEqual(guard.flag_anomalies(rows), [])

    def test_short_fy_not_flagged(self):
        # < MIN_MONTHS aktiva månader → baslinjen ej meningsfull.
        rows = _fy(9, "2026", [8, 3000, 3000])
        self.assertEqual(guard.flag_anomalies(rows), [])

    def test_small_company_base_below_min_not_flagged(self):
        # Pyttebolag: max-månad 345 < MIN_BASELINE(500) → kollaps-flaggor är brus.
        rows = _fy(175, "2025", [6, 33, 345, 300, 280, 310, 290, 320, 300, 305, 295, 312])
        self.assertEqual(guard.flag_anomalies(rows), [])

    def test_future_periods_excluded(self):
        # FY 2026: jan-jun riktiga (3000, 6 stängda månader), jul-dec framtida
        # (~8, ej inträffade). current_period 202607 = "kör i juli".
        rows = _fy(9, "2026", [3000, 3000, 3000, 3000, 3000, 3000, 8, 8, 8, 8, 8, 8])
        # Utan gräns: jul-dec flaggas felaktigt som kollaps.
        self.assertEqual(len(guard.flag_anomalies(rows)), 6)
        # Med innevarande period 202607: jul (innevarande) + aug-dec (framtida)
        # exkluderas → jan-jun (6 stängda) passerar MIN_MONTHS, inga flaggor.
        self.assertEqual(guard.flag_anomalies(rows, current_period="202607"), [])

    def test_current_inprogress_month_excluded(self):
        # Innevarande månad är mid-load (SAF-T laddas efter månadsskiftet) → dess
        # ~0-rader är inte en clobb. jan-jun stängda (3000), jul = innevarande (8).
        rows = _fy(9, "2026", [3000, 3000, 3000, 3000, 3000, 3000, 8])
        # Utan gräns: jul (7:e månaden) flaggas felaktigt som kollaps.
        self.assertEqual([(c, p, n) for c, p, n, _b in guard.flag_anomalies(rows)],
                         [(9, "202607", 8)])
        # Med innevarande period 202607: jul exkluderas (>= gränsen) → inga flaggor.
        self.assertEqual(guard.flag_anomalies(rows, current_period="202607"), [])

    def test_real_clobber_still_flagged_with_current_period(self):
        # Äkta clobb i en passerad månad flaggas fortf. när current_period är satt.
        rows = _fy(9, "2025", [3000, 8, 3000, 3000, 3000, 3000])  # feb clobbad
        flagged = guard.flag_anomalies(rows, current_period="202606")
        self.assertEqual([(c, p, n) for c, p, n, _b in flagged], [(9, "202502", 8)])


if __name__ == "__main__":
    unittest.main()
