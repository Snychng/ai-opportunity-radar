from __future__ import annotations

import json
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from aor_runtime import AorError, atomic_json, installation_context, lock, read_manifest
import aor_runtime


class AorRuntimeTests(unittest.TestCase):
    def test_preflight_checks_once_and_keeps_stdout_clean(self) -> None:
        output, errors = io.StringIO(), io.StringIO()
        with patch.object(aor_runtime, "_preflight_done", False), \
             patch.dict(os.environ, {"AOR_NO_UPDATE_CHECK": "0"}), \
             patch.object(sys, "argv", ["aor", "expand"]), \
             patch("aor_status.check_update", return_value={"status": "update_available", "latest_version": "3.3.0"}) as check, \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            aor_runtime.preflight({"kind": "source"})
            aor_runtime.preflight({"kind": "source"})
        check.assert_called_once()
        self.assertEqual(output.getvalue(), "")
        self.assertIn("aor update", errors.getvalue())

    def test_failed_preflight_never_prevents_research(self) -> None:
        with patch.object(aor_runtime, "_preflight_done", False), \
             patch.dict(os.environ, {"AOR_NO_UPDATE_CHECK": "0"}), \
             patch.object(sys, "argv", ["aor", "expand"]), \
             patch("aor_status.check_update", side_effect=OSError("离线")), \
             patch.object(aor_runtime, "installation_context", return_value={"kind": "source"}):
            self.assertEqual(aor_runtime.run_legacy(lambda: 17, __file__), 17)

    def test_help_preflight_skips_network(self) -> None:
        with patch.object(aor_runtime, "_preflight_done", False), \
             patch.object(sys, "argv", ["aor", "expand", "--help"]), \
             patch("aor_status.check_update") as check:
            aor_runtime.preflight({"kind": "source"})
        check.assert_not_called()

    def test_manifest_identity_and_skill_paths_are_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"name": "ai-opportunity-radar", "version": "3.2.0",
                       "skills": [{"name": "ai-opportunity-radar", "path": "."}]}
            atomic_json(root / "agent-manifest.json", payload)
            self.assertEqual(read_manifest(root)["version"], "3.2.0")
            payload["skills"][0]["path"] = "../outside"
            atomic_json(root / "agent-manifest.json", payload)
            with self.assertRaises(AorError):
                read_manifest(root)
            payload["skills"][0]["path"] = "."
            payload["name"] = "unrelated-project"
            atomic_json(root / "agent-manifest.json", payload)
            with self.assertRaises(AorError):
                read_manifest(root)

    def test_looping_skill_path_is_a_structured_manifest_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "loop").symlink_to("loop")
            atomic_json(root / "agent-manifest.json", {"name": "ai-opportunity-radar", "version": "3.2.0",
                        "skills": [{"name": "ai-opportunity-radar", "path": "loop"}]})
            context = installation_context(root)
            self.assertIsNotNone(context["manifest_error"])
            from aor_status import doctor
            self.assertEqual(doctor(context, offline=True)["health"], "error")

    def test_managed_context_requires_registration_not_directory_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            root = home / "versions" / "v3.2.0-test"
            root.mkdir(parents=True)
            atomic_json(root / "agent-manifest.json", {"name": "ai-opportunity-radar", "version": "3.2.0"})
            (home / "current").symlink_to(root, target_is_directory=True)
            self.assertEqual(installation_context(root)["kind"], "unmanaged")
            atomic_json(home / "install.json", {
                "schema_version": 1, "manager": "aor", "channel": "stable",
                "repository": "https://github.com/Snychng/ai-opportunity-radar.git",
                "bin_path": str(home / "bin" / "aor"),
            })
            context = installation_context(root)
            self.assertEqual(context["kind"], "managed")
            self.assertEqual(context["root"], root.resolve())
            self.assertEqual(context["home"], home.resolve())

    def test_exclusive_update_lock_cannot_interrupt_running_reader(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with lock(Path(tmp), exclusive=False):
                code = "from pathlib import Path; from aor_runtime import lock;\nwith lock(Path(__import__('sys').argv[1]), exclusive=True): pass"
                result = subprocess.run([sys.executable, "-c", code, tmp], capture_output=True, text=True,
                                        env={**os.environ, "PYTHONPATH": str(ROOT / "scripts"), "PYTHONDONTWRITEBYTECODE": "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("正在使用", result.stderr)
            with lock(Path(tmp), exclusive=True):
                pass

    def test_atomic_json_failure_keeps_previous_content(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cache.json"
            atomic_json(target, {"version": 1})
            with patch("aor_runtime.os.replace", side_effect=OSError("模拟失败")):
                with self.assertRaises(OSError):
                    atomic_json(target, {"version": 2})
            self.assertEqual(json.loads(target.read_text()), {"version": 1})
            self.assertEqual([item.name for item in Path(tmp).iterdir()], ["cache.json"])


if __name__ == "__main__":
    unittest.main()
