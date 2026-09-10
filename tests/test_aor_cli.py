from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


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
            self.assertEqual(self.invoke("--version", cwd=base).stdout.strip(), "3.2.0")

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


if __name__ == "__main__":
    unittest.main()
