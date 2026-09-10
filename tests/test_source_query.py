"""来源目录与网页导入的离线行为验证。"""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import aor_bootstrap  # noqa: F401
from aor.sources.importing import import_web_evidence
from aor.sources.registry import diagnose_sources, source_catalog
from contracts import make_run_id


class SourceQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.run_id = make_run_id(as_of=date(2026, 9, 10), mode="targeted_scan", focus=None)
        self.item = {"source": "web", "url": "https://example.com/pricing", "title": "套餐",
                     "original_text": "Pro plan costs $29/month. New users get a trial.",
                     "supporting_quote": "Pro plan costs $29/month.", "evidence_role": "official_pricing",
                     "observed_at": "2026-09-10T09:00:00+08:00",
                     "is_demo": True,
                     "verification": {"verified_by": "host-agent", "verified_at": "2026-09-10T09:00:00+08:00", "method": "opened_page"}}

    def test_catalog_distinguishes_provider_cost_and_manual_only(self) -> None:
        rows = {row["source"]: row for row in source_catalog()}
        self.assertEqual(rows["reddit"]["provider"], "tikhub")
        self.assertEqual(rows["hackernews"]["cost"], "free_public_api")
        self.assertTrue(rows["g2"]["manual_import_only"])
        self.assertFalse(rows["g2"]["default_enabled"])
        self.assertEqual(len([row for row in rows.values() if row["default_enabled"]]), 14)

    def test_configuration_does_not_claim_health_or_call_network(self) -> None:
        with patch("urllib.request.urlopen", side_effect=AssertionError("不能访问网络")):
            output = diagnose_sources({"TIKHUB_API_KEY": True})
        paid = next(row for row in output["sources"] if row["source"] == "reddit")
        self.assertEqual(paid["configuration_status"], "configuration_ready")
        self.assertEqual(paid["live_health"], "not_checked")
        self.assertEqual(output["network_requests"], 0)

    def test_import_pricing_is_traceable_but_does_not_prove_payment(self) -> None:
        with patch("urllib.request.urlopen", side_effect=AssertionError("不能访问网络")):
            result = import_web_evidence({"items": [self.item]}, run_id=self.run_id, as_of="2026-09-10")
        row = result["evidence"][0]
        self.assertEqual(row["signal_types"], ["pricing"])
        self.assertEqual(row["payment_status"], "not_established")
        self.assertEqual(row["original_text"], self.item["original_text"])
        self.assertEqual(row["verification"]["status"], "host_attested")
        self.assertTrue(row["is_demo"])
        self.assertEqual(len(result["input_sha256"]), 64)
        corrected = deepcopy(self.item)
        corrected["original_text"] = "Pro plan costs $39/month."
        corrected["supporting_quote"] = corrected["original_text"]
        revised = import_web_evidence({"items": [corrected]}, run_id=self.run_id, as_of="2026-09-10")["evidence"][0]
        self.assertEqual(row["id"], revised["id"])
        self.assertNotEqual(row["content_sha256"], revised["content_sha256"])

    def test_import_rejects_missing_quote_and_unverified_search_snippet(self) -> None:
        invalid = [dict(self.item, supporting_quote="A customer paid $29"),
                   dict(self.item, verification={**self.item["verification"], "method": "search_snippet"}),
                   dict(self.item, url="https://name:password@example.com"),
                   dict(self.item, payment_status="paid")]
        for item in invalid:
            with self.subTest(item=item), self.assertRaises(ValueError):
                import_web_evidence({"items": [item]}, run_id=self.run_id, as_of="2026-09-10")

    def test_cli_import_reads_back_local_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "input.json"
            target = Path(tmp) / "output.json"
            source.write_text(json.dumps({"items": [self.item]}), encoding="utf-8")
            result = subprocess.run([sys.executable, str(SCRIPTS / "source_query.py"), "import", "--input", str(source),
                                     "--output", str(target), "--run-id", self.run_id, "--as-of", "2026-09-10"],
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            imported = json.loads(target.read_text())
        self.assertEqual(imported["evidence"][0]["url"], self.item["url"])
        self.assertEqual(imported["summary"]["cost_usd"], 0)


if __name__ == "__main__":
    unittest.main()
