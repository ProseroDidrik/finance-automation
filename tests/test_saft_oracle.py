"""Regressions-orakel som unittest (Etapp 4 säkerhetsnät).

Verifierar att SAF-T-parsningen (parse_saft + iter_saft_journal) reproducerar
det frysta fingerprintet i tests/saft_oracle_golden.json över riktiga NO+DK-
filer. Skyddar refaktorn NO → xsdata: byte-för-byte samma parse-kontrakt.

Kräver config.json + riktiga filer under base_path (Didriks maskin). Hoppas
gracefully över annars — golden är committad men data ligger utanför repo:t.

Tunga DK 081 (Actas, 221 MB) ingår bara med SAFT_ORACLE_SLOW=1.

    py -m unittest tests.test_saft_oracle -v
    SAFT_ORACLE_SLOW=1 py -m unittest tests.test_saft_oracle -v
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import saft_regression_oracle as oracle  # noqa: E402


def _files_available() -> bool:
    try:
        from shared import load_config
        base = Path(load_config()["base_path"])
        return (base / "extracted" / "202604").exists()
    except Exception:
        return False


@unittest.skipUnless(oracle.GOLDEN_PATH.exists(), "golden saknas")
@unittest.skipUnless(_files_available(), "riktiga SAF-T-filer saknas under base_path")
class SaftRegressionOracle(unittest.TestCase):
    """parse-kontraktet får inte ändras av refaktorn (NO xsdata, DK iterparse)."""

    def test_fingerprint_matches_golden(self):
        slow = os.environ.get("SAFT_ORACLE_SLOW") == "1"
        mismatches = oracle.verify(slow=slow)
        self.assertEqual(mismatches, [], "\n".join(["SAF-T parse-regression:"] + mismatches))


if __name__ == "__main__":
    unittest.main()
