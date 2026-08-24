"""Tests for the CAPE -> sandbox-forensics-v1 adapter.

The fixture ``fixtures/cape-eicar-report.json`` is a real CAPE 2.5
report.json captured from an EICAR analysis on the freshly-built
sandbox host (task id 1, win11_seabios_101, 2026-04-27). 13 CAPE
behavioral signatures fired plus ClamAV/YARA hits → score 10.0.

Tests run with stdlib only (no pytest required); ``python -m unittest``
discovers them directly.
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

from sandbox_forensics.adapters import UnknownSourceError, normalize
from sandbox_forensics.adapters.cape import _disposition_from_score, _iso
from sandbox_forensics.schema import (
    DISPOSITION_BENIGN,
    DISPOSITION_MALICIOUS,
    DISPOSITION_SUSPICIOUS,
    DISPOSITION_UNKNOWN,
    SCHEMA_VERSION,
)


_FIXTURE_DIR = Path(__file__).parent / "fixtures"
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}([+\-]\d{2}:\d{2}|Z)?$")


def _load_eicar() -> dict:
    with (_FIXTURE_DIR / "cape-eicar-report.json").open() as f:
        return json.load(f)


class IsoTimestampTests(unittest.TestCase):
    def test_cape_format_to_iso(self):
        self.assertEqual(_iso("2026-04-27 19:43:21"), "2026-04-27T19:43:21+00:00")

    def test_already_iso_passthrough(self):
        self.assertTrue(_iso("2026-04-27T19:43:21Z").startswith("2026-04-27"))

    def test_empty_returns_empty(self):
        self.assertEqual(_iso(None), "")
        self.assertEqual(_iso(""), "")

    def test_unparseable_passes_through(self):
        # Don't raise on weird input — adapters should be tolerant
        self.assertEqual(_iso("not-a-date"), "not-a-date")


class DispositionTests(unittest.TestCase):
    def test_high_score_malicious(self):
        self.assertEqual(_disposition_from_score(10.0), [DISPOSITION_MALICIOUS])
        self.assertEqual(_disposition_from_score(5.0), [DISPOSITION_MALICIOUS])

    def test_mid_score_suspicious(self):
        self.assertEqual(_disposition_from_score(4.9), [DISPOSITION_SUSPICIOUS])
        self.assertEqual(_disposition_from_score(3.0), [DISPOSITION_SUSPICIOUS])

    def test_low_score_unknown(self):
        self.assertEqual(_disposition_from_score(2.5), [DISPOSITION_UNKNOWN])
        self.assertEqual(_disposition_from_score(0.5), [DISPOSITION_UNKNOWN])

    def test_zero_score_benign(self):
        self.assertEqual(_disposition_from_score(0.0), [DISPOSITION_BENIGN])


class CapeAdapterEicarTests(unittest.TestCase):
    """Real-CAPE-report tests against a captured EICAR analysis."""

    @classmethod
    def setUpClass(cls):
        cls.native = _load_eicar()
        cls.normalized = normalize(cls.native, source="cape", job_id="test-job-eicar")

    def test_top_level_envelope(self):
        for key in ("schema_version", "analysis", "target", "score", "disposition", "families", "detections"):
            self.assertIn(key, self.normalized, f"missing key {key}")

    def test_schema_version(self):
        self.assertEqual(self.normalized["schema_version"], SCHEMA_VERSION)

    def test_analysis_block(self):
        a = self.normalized["analysis"]
        self.assertEqual(a["id"], "test-job-eicar")
        self.assertEqual(a["source"], "cape")
        self.assertEqual(a["source_task_id"], "1")
        self.assertEqual(a["source_version"], "2.5")
        self.assertGreater(a["duration_seconds"], 0)
        self.assertRegex(a["started_at"], _RFC3339)
        self.assertRegex(a["ended_at"], _RFC3339)
        self.assertEqual(a["environment"]["os"], "windows")
        self.assertEqual(a["environment"]["hostname"], "win11_seabios_101")

    def test_target_block(self):
        t = self.normalized["target"]
        self.assertEqual(t["type"], "file")
        self.assertEqual(
            t["sha256"],
            "131f95c51cc819465fa1797f6ccacf9d494aaaff46fa3eac73ae63ffbdfd8267",
        )
        self.assertEqual(t["size"], 69)
        self.assertEqual(t["filename"], "eicar.txt")
        self.assertTrue(t["sha1"])
        self.assertTrue(t["md5"])

    def test_score_and_disposition(self):
        # CAPE rated EICAR 10/10 → fully malicious
        self.assertEqual(self.normalized["score"], 10.0)
        self.assertIn(DISPOSITION_MALICIOUS, self.normalized["disposition"])

    def test_detections_present(self):
        # 13 CAPE behavioral sigs fired plus ClamAV/YARA contributions
        det = self.normalized["detections"]
        self.assertGreaterEqual(len(det), 13)
        engines = {d["engine"] for d in det}
        # CAPE signatures should always be present for an EICAR run
        self.assertIn("cape_signature", engines)

    def test_detection_shape(self):
        det = self.normalized["detections"][0]
        for key in ("engine", "rule", "severity", "categories", "description"):
            self.assertIn(key, det)
        self.assertIsInstance(det["categories"], list)


class DispatcherTests(unittest.TestCase):
    def test_unknown_source_raises(self):
        with self.assertRaises(UnknownSourceError):
            normalize({}, source="bogus_vendor", job_id="x")


if __name__ == "__main__":
    unittest.main()
