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


if __name__ == "__main__":
    unittest.main()
