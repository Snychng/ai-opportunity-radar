from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import contracts


class ContractRegressionTests(unittest.TestCase):
    def test_stage_envelope_allows_legacy_absence_and_valid_pair(self):
        self.assertEqual(contracts.validate_stage_envelope({}), {"schema_version": "3.0"})
        valid = {"schema_version": "3.0", "run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-10"}
        self.assertEqual(contracts.validate_stage_envelope(valid), valid)

    def test_stage_envelope_rejects_wrong_schema_partial_and_mismatched_dates(self):
        for payload in ({"schema_version": "2.0"}, {"schema_version": None}, {"run_id": None},
                        {"as_of": "2026-09-10"}, {"run_id": "RUN-20260910-ABCDEF1234", "as_of": "2026-09-09"}):
            with self.subTest(payload=payload), self.assertRaises(contracts.ContractError):
                contracts.validate_stage_envelope(payload)

    def test_duplicate_url_cannot_become_independent_by_relabeling(self):
        evidence = [{"source": "a", "url": "https://www.example.com/post/?utm_source=one#reply"},
                    {"source": "b", "url": "https://example.com/post"}]
        self.assertEqual(len(contracts.evidence_independent_sources(evidence)), 1)

    def test_original_publisher_is_shared_across_reposts(self):
        evidence = [{"url": "https://a.example/post", "original_publisher": "作者一"},
                    {"url": "https://b.example/copy", "original_publisher": "作者一"},
                    {"url": "https://c.example/post", "original_publisher": "作者二"}]
        self.assertEqual(len(contracts.evidence_independent_sources(evidence)), 2)

    def test_no_link_or_invalid_link_does_not_count_as_evidence(self):
        self.assertEqual(contracts.evidence_independent_sources([{"source": "one"}, {"url": "not-a-link"}]), set())

    def test_retracted_evidence_cannot_supply_an_independent_source(self):
        evidence = [{"url": "https://a.example/1"}, {"url": "https://b.example/1", "retracted": True},
                    {"url": "https://c.example/1", "status": "retracted"}]
        self.assertEqual(len(contracts.evidence_independent_sources(evidence)), 1)

    def test_source_count_is_order_independent_for_duplicate_url_and_publisher(self):
        evidence = [{"url": "https://a.example/1", "original_publisher": "商家"},
                    {"url": "https://b.example/1", "original_publisher": "商家"},
                    {"url": "https://a.example/1", "source": "换标签"}]
        self.assertEqual(contracts.evidence_independent_sources(evidence),
                         contracts.evidence_independent_sources(list(reversed(evidence))))

    def test_url_normalization_retains_content_query_and_ignores_bad_ports(self):
        self.assertEqual(contracts.canonical_evidence_url("http://WWW.example.com/a/?b=2&a=1&utm_campaign=x#reply"),
                         "https://example.com/a?a=1&b=2")
        self.assertEqual(contracts.canonical_evidence_url("https://example.com:bad/a"), "")
