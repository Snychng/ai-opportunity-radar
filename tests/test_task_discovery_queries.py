"""观察驱动检索的身份、语言、预算和现有 intent 消费边界。"""

from copy import deepcopy
from datetime import date, timedelta
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import aor_bootstrap  # noqa: F401
from aor.sources.industries import DISCOVERY_LENSES, build_industry_discovery, catalog_tasks, load_industries, _task_for
from aor.sources.planning import compile_intents, validate_intent_plan
from aor.sources.task_discovery import build_task_followups, task_followup_intents, MAX_TASKS
from build_query_plan import build_plan

DAY = "2026-09-16"
RUN = "RUN-20260916-1234567890"


def observation(**changes):
    return {"observation_id": "OBS-1", "task_family_id": "TASK-ORDERS", "task": "organize custom orders",
            "target_user": "custom sellers", "need": "remember production details", "language": "en",
            "industry_ids": ["ecommerce"], "feedback_type": "usage", "is_demo": False,
            "trigger": "multiple personalized items", "current_workaround": "copy message details manually",
            "artifact": "one spreadsheet for each order", "desired_outcome": "confirmed production cards",
            "evidence_refs": [{"evidence_id": "EV-1", "revision_id": "REV-1",
                               "quote": "I usually make a spreadsheet for each order."}], **changes}


def followups(*rows, **kwargs):
    return build_task_followups({"observations": list(rows), **kwargs}, as_of=DAY, run_id=RUN)


class TaskDiscoveryQueryTests(unittest.TestCase):
    def test_actual_fields_generate_distinct_queries_and_manual_compatible_intents(self):
        plan = followups(observation())
        self.assertEqual(len(plan["tasks"]), 3)
        self.assertEqual({t["purpose"] for t in plan["tasks"]}, {"workflow_artifact", "alternative", "non_adoption"})
        task = plan["tasks"][0]
        self.assertIn("spreadsheet", task["query"])
        self.assertIn("copy message", task["query"])
        self.assertEqual(task["source_evidence_refs"][0]["revision_id"], "REV-1")
        self.assertEqual(validate_intent_plan(plan["intent_plan"]), task_followup_intents(plan))
        compiled = compile_intents(plan["intent_plan"], as_of=DAY, run_id=RUN)
        self.assertFalse(compiled["tikhub"]["requests"])
        self.assertFalse(compiled["community"]["requests"])
        self.assertEqual(len(compiled["web_import"]["required_imports"]), 3)
        self.assertTrue(all(t["automatic_fetch"] is False for t in compiled["web_import"]["required_imports"]))

    def test_new_observed_artifact_changes_search_not_just_the_label(self):
        first = followups(observation())["tasks"][0]
        second = followups(observation(artifact="printed slips highlighted by production step"))["tasks"][0]
        self.assertNotEqual(first["query"], second["query"])
        self.assertNotEqual(first["query_key"], second["query_key"])

    def test_product_and_delivery_variants_do_not_multiply_family(self):
        base = observation(product="A", solution_hypotheses=[{"form": "app"}])
        other = observation(observation_id="OBS-2", product="B", solution_hypotheses=[{"form": "service"}])
        plan = followups(base, other)
        self.assertEqual(len(plan["tasks"]), 3)
        self.assertEqual(plan["tasks"][0]["source_observation_ids"], ["OBS-1", "OBS-2"])
        self.assertEqual(plan, followups(other, base))
        self.assertEqual([t["query_key"] for t in plan["tasks"]],
                         [t["query_key"] for t in followups(base)["tasks"]])

    def test_repeat_dates_and_completed_keys_do_not_create_template_loops(self):
        plan = followups(observation())
        later = build_task_followups({"observations": [observation()]}, as_of="2026-09-17", run_id="later")
        self.assertEqual(plan["tasks"], later["tasks"])
        repeated = followups(observation(), completed_followup_query_keys=[t["query_key"] for t in plan["tasks"]])
        self.assertFalse(repeated["tasks"])
        self.assertIsNone(repeated["intent_plan"])
        self.assertEqual({s["reason"] for s in repeated["skipped"]}, {"already_completed"})
        with self.assertRaises(ValueError):
            build_task_followups(plan, as_of=DAY, run_id=RUN)

    def test_chinese_task_cannot_be_mislabeled_english_and_unknown_stays_unknown(self):
        chinese = followups(observation(task="整理定制订单", artifact="每个订单一个表格", current_workaround="手动复制聊天记录",
                                       desired_outcome="制作单", trigger="多个定制要求", language="en"))
        self.assertEqual({task["language"] for task in chinese["tasks"]}, {"unknown"})
        unknown = followups(observation(language="unknown"))
        self.assertEqual({task["language"] for task in unknown["tasks"]}, {"unknown"})
        japanese = followups(observation(task="家族の料理を保存する", language="unknown", artifact="料理日記"))
        self.assertEqual({task["language"] for task in japanese["tasks"]}, {"unknown"})
        declared = followups(observation(task="整理定制订单", language="zh", artifact="订单表格"))
        self.assertEqual({task["language"] for task in declared["tasks"]}, {"zh"})

    def test_non_direct_demo_and_unbound_input_cannot_seed_followups(self):
        plan = followups(observation(is_demo=True), observation(feedback_type="promotion"),
                         observation(evidence_refs=[{"evidence_id": "EV-1", "quote": "text"}]))
        self.assertFalse(plan["tasks"])
        self.assertEqual(len(plan["skipped"]), 3)
        self.assertFalse(plan["summary"]["market_validated"])

    def test_positive_motivation_does_not_require_a_product_or_purchase(self):
        plan = followups(observation(feedback_type="positive_behavior", task="preserve family recipes",
                                    artifact="grandmother handwritten cookbook"))
        self.assertEqual(len(plan["tasks"]), 3)
        self.assertTrue(all("existing_alternative" in task["required_checks"] and
                            "non_adoption_reason" in task["required_checks"] for task in plan["tasks"]))

    def test_limited_followups_keep_explicit_industry_and_language_diversity(self):
        rows = [observation(observation_id=f"OBS-{i}", task_family_id=f"TASK-{i}", task=f"order task {i}")
                for i in range(12)]
        rows.extend([observation(observation_id="OBS-ZH", task_family_id="TASK-ZH", task="搬家装箱找物", language="zh",
                                 industry_ids=["personal_life"], artifact="箱子清单"),
                     observation(observation_id="OBS-GAME", task_family_id="TASK-GAME", task="record campaign notes",
                                 industry_ids=["gaming"])])
        tasks = followups(*rows)["tasks"]
        self.assertEqual({industry for task in tasks for industry in task["industry_ids"]},
                         {"ecommerce", "personal_life", "gaming"})
        self.assertEqual({task["language"] for task in tasks}, {"en", "zh"})

    def test_families_queries_and_lengths_are_bounded_without_mutating_input(self):
        rows = [observation(observation_id=f"OBS-{i}", task_family_id=f"TASK-{i}", task=f"task {i} " * 30)
                for i in range(20)]
        before = deepcopy(rows)
        plan = followups(*rows)
        self.assertLessEqual(len(plan["tasks"]), MAX_TASKS)
        self.assertLessEqual(plan["summary"]["planned_task_family_count"], 8)
        self.assertTrue(all(len(task["query"]) <= 100 for task in plan["tasks"]))
        self.assertEqual(rows, before)
        self.assertTrue(any(row["reason"] == "family_budget" for row in plan["skipped"]))

    def test_default_catalog_rotates_all_legacy_and_new_tasks_under_same_budget(self):
        catalog = load_industries()
        self.assertEqual(sum(len(row["discovery_entries"]) for row in catalog), 30)
        self.assertEqual(sum(len(row["subtracks"]) for row in catalog), 36)
        self.assertEqual(sum(len(catalog_tasks(row)) for row in catalog), 66)
        with tempfile.TemporaryDirectory() as temp:
            observed, task_ids = {}, {}
            for offset in range(11):
                day = date.fromisoformat(DAY) + timedelta(days=offset)
                result = build_industry_discovery(as_of=day, run_id=f"RUN-{day:%Y%m%d}-1234567890", home=Path(temp), preferences={},
                                                 planned_sources=["reddit", "xiaohongshu"])
                paid = result["retrieval_plans"]["tikhub"]
                self.assertEqual(len(paid["requests"]), 12)
                self.assertEqual(len(result["retrieval_plans"]["web_import"]["required_imports"]), 6)
                for task in paid["research_tasks"]:
                    observed.setdefault(task["industry_ids"][0], set()).add(task["discovery_lens"])
                    task_ids.setdefault((task["industry_ids"][0], task["language"]), set()).add(task["task_id"])
                    self.assertEqual(task["hypothesis_status"], "retrieval_seed_not_observed_demand")
                    self.assertIn("non_adoption_reason", task["required_checks"])
            self.assertTrue(all(lenses == {*DISCOVERY_LENSES, "legacy_seed"} for lenses in observed.values()))
            for row in catalog:
                expected = {f"{row['id']}.{task['id']}" for task in [*row['subtracks'], *row['discovery_entries']]}
                for language in ("en", "zh"):
                    self.assertEqual(task_ids[(row["id"], language)], expected)

    def test_legacy_catalog_keeps_original_rotation_and_order(self):
        for original in load_industries():
            row = deepcopy(original)
            row.pop("discovery_entries")
            row["catalog_version"] = "2.0"
            self.assertEqual(catalog_tasks(row), row["subtracks"])
            for offset in range(len(row["subtracks"])):
                day = date.fromisoformat(DAY) + timedelta(days=offset)
                task, state = _task_for(row, "en", day, {})
                self.assertEqual(task, row["subtracks"][day.toordinal() % len(row["subtracks"])])
                self.assertEqual(state, {})

    def test_explicit_followup_industries_populate_only_claimed_scope(self):
        with tempfile.TemporaryDirectory() as temp:
            plan = build_plan(date.fromisoformat(DAY), Path(temp), intent_plan=followups(observation())["intent_plan"])
        self.assertEqual(plan["selected_industries"], ["ecommerce"])
        self.assertTrue(plan["industry_catalog"])

    def test_new_coverage_snapshot_preserves_actual_history_rotation(self):
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            kwargs = dict(as_of=date.fromisoformat(DAY), run_id=RUN, home=home, preferences={},
                          planned_sources=["reddit", "xiaohongshu"])
            before = build_industry_discovery(**kwargs)["retrieval_plans"]["tikhub"]["research_tasks"][0]
            directory = home / "runs/older"
            directory.mkdir(parents=True)
            (directory / "industry-coverage.json").write_text(json.dumps({"version": "2.1", "as_of": "2026-09-15",
                "tasks": [{"task_id": before["task_id"], "language": before["language"], "request_count": 1,
                           "review_gaps": ["alternative"]}]}))
            after = build_industry_discovery(**kwargs)["retrieval_plans"]["tikhub"]["research_tasks"][0]
            self.assertEqual(after["task_id"], before["task_id"])
            self.assertEqual(after["prior_attempt_count"], 1)
            self.assertEqual(after["selection_reason"], "unresolved_evidence_gap")


if __name__ == "__main__":
    unittest.main()
