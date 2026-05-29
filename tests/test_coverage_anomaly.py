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


if __name__ == "__main__":
    unittest.main()
