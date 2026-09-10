from __future__ import annotations

import json
import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import radar


class AorCliTests(unittest.TestCase):
    def invoke(self, *args: str, cwd: Path, entry: str = "bin/aor") -> subprocess.CompletedProcess[str]:
        return subprocess.run([sys.executable, str(ROOT / entry), *args], cwd=cwd, capture_output=True,
                              text=True, check=False, env={**os.environ, "AOR_OFFLINE": "1",
                                                         "AOR_CACHE_HOME": str(cwd / "cache"),
                                                         "PYTHONDONTWRITEBYTECODE": "1"})

    def test_global_entry_works_outside_repository_and_lists_only_owned_skills(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = self.invoke("skills", "--json", cwd=base)
            self.assertEqual(result.returncode, 0, result.stderr)
            skills = json.loads(result.stdout)
            self.assertEqual([row["name"] for row in skills], ["ai-opportunity-radar"])
            self.assertEqual(Path(skills[0]["path"]).resolve(), ROOT)
            expected = json.loads((ROOT / "agent-manifest.json").read_text())["version"]
            self.assertEqual(self.invoke("--version", cwd=base).stdout.strip(), expected)

    def quiet_doctor(self, result: dict) -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        with patch("aor_status.doctor", return_value=result), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = radar.main(["doctor", "--quiet"])
        return code, output.getvalue(), errors.getvalue()

    def test_quiet_doctor_is_silent_without_confirmed_new_version(self) -> None:
        for status in ("up_to_date", "ahead", "unknown"):
            with self.subTest(status=status):
                result = {"health": "warning" if status == "unknown" else "ok",
                          "updates": {"status": status, "latest_version": "3.2.1"}, "checks": []}
                self.assertEqual(self.quiet_doctor(result), (0, "", ""))

    def test_quiet_doctor_notifies_version_and_update_command(self) -> None:
        result = {"health": "warning", "updates": {"status": "update_available", "latest_version": "3.2.2"},
                  "checks": [{"name": "updates", "status": "warning", "message": "有更新"}]}
        code, output, errors = self.quiet_doctor(result)
        self.assertEqual((code, output), (0, ""))
        self.assertIn("v3.2.2", errors)
        self.assertIn("aor update", errors)
        self.assertEqual(len(errors.splitlines()), 1)

    def test_quiet_doctor_keeps_actionable_local_errors_visible(self) -> None:
        result = {"health": "error", "updates": {"status": "up_to_date", "latest_version": "3.2.1"},
                  "checks": [{"name": "manifest", "status": "error", "message": "技能清单损坏"}]}
        code, output, errors = self.quiet_doctor(result)
        self.assertEqual((code, output), (1, ""))
        self.assertIn("技能清单损坏", errors)
        self.assertNotIn("up_to_date", errors)
        self.assertNotIn("aor update", errors)

    def test_quiet_offline_doctor_works_outside_repository(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = self.invoke("doctor", "--quiet", "--offline", cwd=base)
            self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))
            self.assertFalse((base / "cache").exists())

    def test_overview_only_notifies_available_updates(self) -> None:
        for status in ("up_to_date", "update_available"):
            output, errors = io.StringIO(), io.StringIO()
            with patch("aor_status.check_update", return_value={"status": status, "latest_version": "3.2.2"}), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                self.assertEqual(radar.main([]), 0)
            self.assertNotIn("up_to_date", output.getvalue())
            if status == "update_available":
                self.assertIn("v3.2.2", errors.getvalue())
                self.assertIn("aor update", errors.getvalue())
            else:
                self.assertEqual(errors.getvalue(), "")

    def test_offline_doctor_separates_local_health_from_network_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            result = self.invoke("doctor", "--offline", "--json", cwd=base)
            self.assertEqual(result.returncode, 0, result.stderr)
            value = json.loads(result.stdout)
            self.assertIn(value["health"], {"ok", "warning"})
            self.assertEqual(value["updates"]["status"], "unknown")
            self.assertFalse((base / "cache").exists())

    def test_existing_research_command_keeps_stdout_machine_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            args = ("expand", "--input", str(ROOT / "examples" / "benchmarks-and-dimensions.json"), "--limit", "3")
            current = self.invoke(*args, cwd=base)
            legacy = self.invoke(*args, cwd=base, entry="scripts/radar.py")
            self.assertEqual(current.returncode, 0, current.stderr)
            self.assertEqual(json.loads(current.stdout), json.loads(legacy.stdout))
            self.assertEqual(len(json.loads(current.stdout)["candidates"]), 3)

    def test_management_help_never_installs_or_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for command in ("install", "update", "doctor", "skills"):
                result = self.invoke(command, "--help", cwd=base)
                self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(base.iterdir()), [])

    def test_global_json_flag_is_forwarded_to_management(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            result = self.invoke("--json", "doctor", "--offline", cwd=Path(tmp))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("health", json.loads(result.stdout))

    def test_doctor_reports_broken_skill_link_as_json_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "scripts").mkdir()
            for name in ("radar.py", "aor_runtime.py", "aor_status.py"):
                shutil.copyfile(ROOT / "scripts" / name, base / "scripts" / name)
            manifest = json.loads((ROOT / "agent-manifest.json").read_text())
            manifest["skills"][0]["path"] = "loop"
            (base / "agent-manifest.json").write_text(json.dumps(manifest))
            (base / "loop").symlink_to("loop")
            result = self.invoke("doctor", "--offline", "--json", cwd=base, entry=str(base / "scripts" / "radar.py"))
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(result.stdout)["health"], "error")
            self.assertNotIn("Traceback", result.stderr)

    def test_json_doctor_and_research_work_without_display_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            shutil.copytree(ROOT / "scripts", base / "scripts", ignore=shutil.ignore_patterns("aor_display.py", "__pycache__"))
            shutil.copytree(ROOT / "src", base / "src", ignore=shutil.ignore_patterns("__pycache__"))
            (base / "bin").mkdir()
            for relative in ("agent-manifest.json", "SKILL.md", "bin/aor"):
                shutil.copyfile(ROOT / relative, base / relative)
            commands = (("--json",), ("skills", "--json"), ("doctor", "--offline", "--json"),
                        ("doctor", "--offline"),
                        ("expand", "--input", str(ROOT / "examples" / "benchmarks-and-dimensions.json"),
                         "--limit", "3"))
            self.assertFalse((base / "scripts" / "aor_display.py").exists())
            for arguments in commands:
                with self.subTest(arguments=arguments):
                    result = self.invoke(*arguments, cwd=base, entry=str(base / "bin" / "aor"))
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stderr, "")
                    if arguments != ("doctor", "--offline"):
                        self.assertIsInstance(json.loads(result.stdout), (dict, list))


class AorPresentationCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = {"version": "3.2.2", "root": Path("/private/source/long/路径"),
                        "commit": "a" * 40, "kind": "source"}
        self.skills = [{"name": "ai-opportunity-radar", "path": "/private/source/long/路径/SKILL.md",
                        "description": "创业机会研究与个人验证", "status": "ok", "message": "技能入口可用"}]

    def invoke_presentation(self, *arguments: str, update_status: str = "up_to_date") -> tuple[int, str, str]:
        output, errors = io.StringIO(), io.StringIO()
        updates = {"status": update_status, "latest_version": "9.8.7"}
        status = {"name": "ai-opportunity-radar", "version": self.context["version"],
                  "root": str(self.context["root"]), "current_path": str(self.context["root"]),
                  "kind": "source", "commit": self.context["commit"]}
        diagnostic = {"health": "ok", "installation": status, "skills": self.skills,
                      "updates": updates, "checks": []}
        with patch("radar.installation_context", return_value=self.context), \
             patch("aor_status.get_status", return_value=status), \
             patch("aor_status.list_skills", return_value=self.skills), \
             patch("aor_status.check_update", return_value=updates), \
             patch("aor_status.doctor", return_value=diagnostic), \
             patch.dict(os.environ, {"AOR_OFFLINE": "1", "TERM": "xterm-256color", "COLUMNS": "80"}), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            code = radar.main(list(arguments))
        return code, output.getvalue(), errors.getvalue()

    def test_human_overview_hides_internal_details_and_sends_new_version_only_to_stderr(self) -> None:
        for status in ("up_to_date", "update_available"):
            with self.subTest(status=status):
                code, output, errors = self.invoke_presentation(update_status=status)
                self.assertEqual(code, 0)
                self.assertIn("AI Opportunity Radar", output)
                self.assertIn("3.2.2", output)
                self.assertIn("ai-opportunity-radar", output)
                self.assertNotIn(str(self.context["root"]), output)
                self.assertNotIn(self.context["commit"], output)
                self.assertNotIn("9.8.7", output)
                self.assertNotIn("\x1b", output)
                if status == "update_available":
                    self.assertIn("9.8.7", errors)
                    self.assertIn("aor update", errors)
                    self.assertEqual(len(errors.splitlines()), 1)
                else:
                    self.assertEqual(errors, "")

    def test_human_skills_keeps_description_and_version_without_full_path(self) -> None:
        code, output, errors = self.invoke_presentation("skills")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(self.skills[0]["name"], output)
        self.assertIn(self.skills[0]["description"], output)
        self.assertIn("3.2.2", output)
        self.assertNotIn(self.skills[0]["path"], output)
        self.assertNotIn(self.context["commit"], output)
        self.assertNotIn("\x1b", output)

    def test_overview_json_keeps_complete_details_without_display_or_update_notice(self) -> None:
        code, output, errors = self.invoke_presentation("--json", update_status="update_available")
        self.assertEqual((code, errors), (0, ""))
        value = json.loads(output)
        self.assertEqual(value["root"], str(self.context["root"]))
        self.assertEqual(value["current_path"], str(self.context["root"]))
        self.assertEqual(value["commit"], self.context["commit"])
        self.assertEqual(value["skills"], self.skills)
        self.assertEqual(value["updates"], {"status": "update_available", "latest_version": "9.8.7"})

    def test_skills_json_preserves_complete_records_for_both_flag_positions(self) -> None:
        for arguments in (("skills", "--json"), ("--json", "skills")):
            with self.subTest(arguments=arguments):
                code, output, errors = self.invoke_presentation(*arguments)
                self.assertEqual((code, errors), (0, ""))
                self.assertEqual(json.loads(output), self.skills)

    def test_doctor_remains_the_place_to_inspect_full_installation_details(self) -> None:
        code, output, errors = self.invoke_presentation("doctor", "--offline")
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(str(self.context["root"]), output)
        code, output, errors = self.invoke_presentation("doctor", "--offline", "--json")
        self.assertEqual((code, errors), (0, ""))
        value = json.loads(output)
        self.assertEqual(value["installation"]["root"], str(self.context["root"]))
        self.assertEqual(value["installation"]["commit"], self.context["commit"])
        self.assertEqual(value["skills"], self.skills)


if __name__ == "__main__":
    unittest.main()
