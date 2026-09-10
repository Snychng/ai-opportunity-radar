from __future__ import annotations

import copy
import io
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import aor_status


class Response(io.BytesIO):
    def geturl(self) -> str:
        return aor_status.LATEST_RELEASE_URL


class StatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        (self.project / "scripts").mkdir()
        (self.project / "bin").mkdir()
        (self.project / "bin" / "aor").write_text("#!/usr/bin/env python3\n")
        (self.project / "scripts" / "radar.py").write_text("pass\n")
        (self.project / "SKILL.md").write_text("# 测试技能\n")
        self.manifest = {
            "name": "ai-opportunity-radar", "version": "3.2.0", "instructions": "SKILL.md",
            "skills": [{"name": "ai-opportunity-radar", "path": ".", "description": "寻找创业机会"}],
        }
        self.write_manifest()
        self.context = {
            "root": self.project, "home": self.base / "managed", "kind": "managed",
            "version": "3.2.0", "commit": "a" * 40,
            "repository": "https://github.com/Snychng/ai-opportunity-radar.git", "channel": "stable",
            "current_path": self.project,
        }
        self.cache = self.base / "cache"
        self.addCleanup(patch.stopall)
        patch.object(aor_status, "CACHE_HOME", self.cache).start()
        self.opener = Mock()
        patch.object(aor_status, "build_opener", return_value=self.opener).start()
        self.release = {
            "tag_name": "v3.3.0", "draft": False, "prerelease": False,
            "html_url": "https://github.com/Snychng/ai-opportunity-radar/releases/tag/v3.3.0",
        }
        self.set_release(self.release)

    def write_manifest(self) -> None:
        (self.project / "agent-manifest.json").write_text(json.dumps(self.manifest))

    def set_release(self, release: dict) -> None:
        self.opener.open.side_effect = lambda *args, **kwargs: Response(json.dumps(release).encode())

    def test_status_is_local_and_shows_actual_installation(self) -> None:
        result = aor_status.get_status(self.context)
        self.assertEqual(result["version"], "3.2.0")
        self.assertEqual(result["root"], str(self.project))
        self.assertTrue(result["update_managed"])
        self.opener.open.assert_not_called()
        self.context.update(kind="source", home=None)
        self.assertFalse(aor_status.get_status(self.context)["update_managed"])

    def test_skills_reads_only_registered_project_directories(self) -> None:
        extra = self.project / "unregistered"
        extra.mkdir()
        (extra / "SKILL.md").write_text("不应发现")
        rows = aor_status.list_skills(self.context)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "ok")
        self.assertEqual(rows[0]["path"], str(self.project))

    def test_skills_rejects_traversal_absolute_paths_and_symlink_escape(self) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("不应读取")
        (self.project / "escape").symlink_to(outside, target_is_directory=True)
        for path in ("../outside", str(outside), "escape"):
            with self.subTest(path=path):
                self.manifest["skills"] = [{"name": "outside", "path": path}]
                self.write_manifest()
                self.assertEqual(aor_status.list_skills(self.context)[0]["status"], "error")

    def test_registered_skill_cannot_link_instructions_outside_project(self) -> None:
        outside = self.base / "outside.md"
        outside.write_text("不应读取")
        (self.project / "SKILL.md").unlink()
        (self.project / "SKILL.md").symlink_to(outside)
        self.assertEqual(aor_status.list_skills(self.context)[0]["status"], "error")
        self.assertEqual(aor_status.doctor(self.context, offline=True)["health"], "error")

    def test_skills_reports_invalid_registry_missing_and_duplicate_names(self) -> None:
        for entries in (None, [], [None], [{"name": "missing", "path": "missing"}],
                        [{"name": "x", "path": "."}, {"name": "x", "path": "."}]):
            with self.subTest(entries=entries):
                self.manifest["skills"] = entries
                self.write_manifest()
                self.assertIn("error", [row["status"] for row in aor_status.list_skills(self.context)])

    def test_check_fetches_official_stable_release_without_credentials(self) -> None:
        result = aor_status.check_update(self.context)
        self.assertEqual(result["status"], "update_available")
        self.assertEqual(result["latest_version"], "3.3.0")
        self.assertEqual(result["tag_name"], "v3.3.0")
        self.assertFalse(result["from_cache"])
        args, kwargs = self.opener.open.call_args
        self.assertEqual(args[0].full_url, aor_status.LATEST_RELEASE_URL)
        self.assertEqual(kwargs["timeout"], 2.5)
        self.assertNotIn("Authorization", args[0].headers)
        self.assertNotIn("Cookie", args[0].headers)

    def test_release_check_respects_default_proxy_configuration(self) -> None:
        with patch.object(aor_status, "ProxyHandler") as proxy_handler:
            result = aor_status.check_update(self.context, refresh=True)
        proxy_handler.assert_called_once_with()
        self.assertEqual(result["status"], "update_available")

    def test_version_comparison_is_numeric_and_never_suggests_downgrade(self) -> None:
        for current, latest, expected in (("3.2.0", "3.2.0", "up_to_date"),
                                          ("3.10.0", "3.9.0", "ahead"),
                                          ("3.9.0", "3.10.0", "update_available"),
                                          ("invalid", "3.3.0", "unknown")):
            with self.subTest(current=current, latest=latest):
                self.context["version"] = current
                value = copy.deepcopy(self.release)
                value.update(tag_name="v" + latest,
                             html_url="https://github.com/Snychng/ai-opportunity-radar/releases/tag/v" + latest)
                self.set_release(value)
                self.assertEqual(aor_status.check_update(self.context, refresh=True)["status"], expected)

    def test_fresh_cache_avoids_network_and_offline_never_writes(self) -> None:
        first = aor_status.check_update(self.context)
        self.opener.open.reset_mock()
        cached = aor_status.check_update(self.context)
        self.assertTrue(cached["from_cache"])
        self.assertEqual(cached["checked_at"], first["checked_at"])
        self.opener.open.assert_not_called()
        snapshots = {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}
        self.assertTrue(aor_status.check_update(self.context, refresh=True, offline=True)["from_cache"])
        self.assertEqual(snapshots, {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()})
        self.opener.open.assert_not_called()

    def test_offline_without_cache_does_not_create_cache_directory(self) -> None:
        result = aor_status.check_update(self.context, offline=True)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(self.cache.exists())
        self.opener.open.assert_not_called()

    def test_cache_is_bound_to_root_commit_repository_and_channel(self) -> None:
        aor_status.check_update(self.context)
        for field, value in (("root", self.base / "another"), ("commit", "b" * 40),
                             ("repository", "https://example.org/other.git"), ("channel", "preview")):
            with self.subTest(field=field):
                context = {**self.context, field: value}
                self.assertEqual(aor_status.check_update(context, offline=True)["status"], "unknown")

    def test_refresh_fetches_again_and_network_error_does_not_replace_valid_cache(self) -> None:
        aor_status.check_update(self.context)
        before = {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}
        self.opener.open.side_effect = URLError("测试网络中断")
        result = aor_status.check_update(self.context, refresh=True)
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["from_cache"])
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))

    def test_network_failure_is_briefly_cached_as_unknown_and_refresh_bypasses_it(self) -> None:
        self.opener.open.side_effect = URLError("测试网络中断")
        first = aor_status.check_update(self.context)
        second = aor_status.check_update(self.context)
        self.assertEqual(first["status"], "unknown")
        self.assertEqual(second["status"], "unknown")
        self.assertTrue(second["from_cache"])
        self.assertEqual(self.opener.open.call_count, 1)
        self.set_release(self.release)
        self.assertEqual(aor_status.check_update(self.context, refresh=True)["status"], "update_available")

    def test_stale_future_and_corrupt_cache_are_not_current(self) -> None:
        aor_status.check_update(self.context)
        paths = [p for p in self.cache.rglob("*.json")]
        self.assertEqual(len(paths), 1)
        value = json.loads(paths[0].read_text())
        for delta in (timedelta(hours=-25), timedelta(days=1)):
            modified = {**value, "checked_at": (datetime.now(timezone.utc) + delta).isoformat()}
            paths[0].write_text(json.dumps(modified))
            self.assertEqual(aor_status.check_update(self.context, offline=True)["status"], "unknown")
        paths[0].write_text("not json")
        self.assertEqual(aor_status.check_update(self.context, offline=True)["status"], "unknown")

    def test_tampered_cache_does_not_bypass_release_validation(self) -> None:
        aor_status.check_update(self.context)
        path = next(self.cache.rglob("*.json"))
        value = json.loads(path.read_text())
        value["release"]["html_url"] = "https://evil.example/download"
        path.write_text(json.dumps(value))
        result = aor_status.check_update(self.context, offline=True)
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["release_url"])

    def test_unsupported_channel_never_fetches_a_different_feed(self) -> None:
        self.context["channel"] = "preview"
        self.assertEqual(aor_status.check_update(self.context)["status"], "unknown")
        self.opener.open.assert_not_called()

    def test_missing_release_and_service_failure_remain_unknown(self) -> None:
        for code in (404, 403, 500):
            with self.subTest(code=code):
                self.opener.open.side_effect = HTTPError(aor_status.LATEST_RELEASE_URL, code, "测试", {}, None)
                result = aor_status.check_update(self.context, refresh=True)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["latest_version"])

    def test_untrusted_release_payloads_do_not_become_updates_or_cache(self) -> None:
        changes = (
            {"draft": True}, {"prerelease": True}, {"draft": "false"}, {"tag_name": "v3.4.0-beta.1"},
            {"tag_name": "../../v3.4.0"}, {"html_url": "https://evil.example/v3.3.0"},
            {"html_url": "https://github.com/Snychng/other/releases/tag/v3.3.0"},
            {"html_url": "https://github.com/Snychng/ai-opportunity-radar/releases/tag/v3.2.0"},
        )
        for change in changes:
            with self.subTest(change=change):
                self.set_release({**self.release, **change})
                self.assertEqual(aor_status.check_update(self.context, refresh=True)["status"], "unknown")
                self.assertFalse(self.cache.exists())

    def test_malformed_or_oversized_response_is_unknown(self) -> None:
        for data in (b"not-json", b"[]", b"x" * (aor_status.MAX_RESPONSE_BYTES + 1)):
            with self.subTest(length=len(data)):
                self.opener.open.side_effect = lambda *args, **kwargs: Response(data)
                self.assertEqual(aor_status.check_update(self.context, refresh=True)["status"], "unknown")
                self.assertFalse(self.cache.exists())

    def test_redirect_handler_rejects_cross_origin_downgrade_and_userinfo(self) -> None:
        handler = aor_status.ReleaseRedirectHandler()
        request = Request(aor_status.LATEST_RELEASE_URL)
        for url in ("http://api.github.com/releases", "https://evil.example/releases",
                    "https://user:password@api.github.com/releases", "https://api.github.com:444/releases"):
            with self.subTest(url=url), self.assertRaises(URLError):
                handler.redirect_request(request, None, 302, "redirect", {}, url)
        self.assertIsNotNone(handler.redirect_request(
            request, None, 302, "redirect", {}, "https://api.github.com/repos/Snychng/ai-opportunity-radar/releases/1"))

    def test_failed_cache_write_does_not_block_successful_update_check(self) -> None:
        with patch.object(aor_status, "atomic_json", side_effect=OSError("只读缓存目录")):
            result = aor_status.check_update(self.context)
        self.assertEqual(result["status"], "update_available")

    def test_doctor_reports_local_defects_without_hiding_unknown_update(self) -> None:
        with patch.object(aor_status.shutil, "which", return_value="/usr/bin/git"):
            result = aor_status.doctor(self.context, offline=True)
        self.assertEqual(result["health"], "warning")
        self.assertEqual(result["updates"]["status"], "unknown")
        self.assertEqual(result["installation"]["root"], str(self.project))
        (self.project / "scripts" / "radar.py").unlink()
        with patch.object(aor_status.shutil, "which", return_value=None):
            result = aor_status.doctor(self.context, offline=True)
        self.assertEqual(result["health"], "error")
        self.assertTrue(any(row["status"] == "error" for row in result["checks"]))

    def test_doctor_handles_invalid_manifest_and_unmanaged_installation(self) -> None:
        (self.project / "agent-manifest.json").write_text("invalid")
        self.context.update(kind="unmanaged", home=None)
        result = aor_status.doctor(self.context, offline=True)
        self.assertEqual(result["health"], "error")
        self.assertFalse(result["installation"]["update_managed"])

    def test_doctor_warns_when_agent_still_loads_previous_managed_version(self) -> None:
        self.context["is_current"] = False
        result = aor_status.doctor(self.context, offline=True)
        self.assertFalse(result["installation"]["is_current"])
        self.assertTrue(any(item["name"] == "loaded_version" and item["status"] == "warning"
                            for item in result["checks"]))

    def test_doctor_warns_about_local_changes_without_suggesting_overwrite(self) -> None:
        for kind in ("managed", "source"):
            with self.subTest(kind=kind):
                self.context.update(kind=kind, worktree_clean=False, origin_verified=True)
                result = aor_status.doctor(self.context, offline=True)
                self.assertFalse(result["installation"]["worktree_clean"])
                check = next(item for item in result["checks"] if item["name"] == "worktree")
                self.assertEqual(check["status"], "warning")
                self.assertIn("不会覆盖", check["message"])

    def test_managed_installation_requires_verified_official_origin(self) -> None:
        self.context.update(origin_verified=False, worktree_clean=True)
        result = aor_status.doctor(self.context, offline=True)
        self.assertEqual(result["health"], "error")
        self.assertFalse(result["installation"]["origin_verified"])
        check = next(item for item in result["checks"] if item["name"] == "origin")
        self.assertEqual(check["status"], "error")

    def test_clean_worktree_and_matching_version_do_not_claim_commit_identity(self) -> None:
        self.context.update(origin_verified=True, worktree_clean=True)
        value = {**self.release, "tag_name": "v3.2.0",
                 "html_url": "https://github.com/Snychng/ai-opportunity-radar/releases/tag/v3.2.0"}
        self.set_release(value)
        result = aor_status.doctor(self.context, refresh=True)
        self.assertTrue(result["installation"]["worktree_clean"])
        self.assertEqual(result["updates"]["status"], "up_to_date")
        self.assertIn("版本号", result["updates"]["reason"])
        self.assertFalse(any(item["name"] == "worktree" and item["status"] != "ok" for item in result["checks"]))


if __name__ == "__main__":
    unittest.main()
