"""同批恢复复用已固定的发现计划，不能重选来源或丢弃重试授权。"""
from datetime import date
import json
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.workflow.research import run_discovery, start_research
from tests.test_tikhub_query import pricing_rows


class DiscoveryResumeTests(unittest.TestCase):
    def test_resume_keeps_original_plan_and_explicit_request_authorizations(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"AOR_OFFLINE": "0"}), \
                patch("aor.workflow.research._collect"):
            home = Path(temp)
            run = start_research(home, as_of=date(2026, 9, 14), offline=False)
            with patch("tikhub_query.fetch_live_pricing", return_value=pricing_rows()), \
                    patch("aor.workflow.research.run_paid_batch", return_value={"status": "fixture"}) as execute:
                run_discovery(home, run["run_id"], max_cost_usd=1, batch_id="fixed", max_requests=2)
                path = execute.call_args.args[2]
            before = path.read_bytes()
            plan_before = json.loads((Path(run["run_path"]) / "plan.json").read_text())
            with patch("tikhub_query.fetch_live_pricing", side_effect=AssertionError("恢复不能重新选来源")), \
                    patch("aor.workflow.research.run_paid_batch", return_value={"status": "fixture"}) as execute:
                run_discovery(home, run["run_id"], max_cost_usd=1, batch_id="fixed", resume=True,
                              resolve_unknown=("request-a",), retry_failed=("request-b",), max_attempts=2)
                self.assertTrue(execute.call_args.kwargs["resume"])
                self.assertEqual(execute.call_args.kwargs["resolve_unknown"], ("request-a",))
                self.assertEqual(execute.call_args.kwargs["retry_failed"], ("request-b",))
                self.assertEqual(execute.call_args.kwargs["max_attempts"], 2)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(json.loads((Path(run["run_path"]) / "plan.json").read_text()), plan_before)
            with self.assertRaisesRegex(ValueError, "resume-batch"):
                run_discovery(home, run["run_id"], max_cost_usd=1, batch_id="fixed")


if __name__ == "__main__":
    unittest.main()
