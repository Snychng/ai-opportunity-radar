from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import aor_install


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.name=AOR Test", "-c", "user.email=test@example.invalid", *args],
        cwd=root, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def repository(root: Path, version: str = "3.2.0") -> Path:
    root.mkdir()
    (root / "scripts").mkdir()
    (root / "bin").mkdir()
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    (root / "SKILL.md").write_text("---\nname: ai-opportunity-radar\n---\n测试技能\n", encoding="utf-8")
    (root / "agent-manifest.json").write_text(json.dumps({
        "name": "ai-opportunity-radar", "version": version, "instructions": "SKILL.md",
        "entrypoint": ["python3", "scripts/radar.py"],
    }), encoding="utf-8")
    (root / "scripts" / "radar.py").write_text("print('{\"health\": \"ok\"}')\n", encoding="utf-8")
    for name in ("aor_runtime.py", "aor_status.py", "aor_install.py"):
        (root / "scripts" / name).write_text("# 测试模块\n", encoding="utf-8")
    (root / "bin" / "aor").write_text("#!/usr/bin/env python3\nprint('AOR 测试入口')\n", encoding="utf-8")
    git(root, "init", "-q")
    git(root, "remote", "add", "origin", aor_install.OFFICIAL_REPOSITORY)
    git(root, "add", ".")
    git(root, "commit", "-qm", "测试版本")
    git(root, "tag", f"v{version}")
    return root


class InstallationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.source = repository(self.base / "source")
        self.home = self.base / "managed"
        self.bin_dir = self.base / "bin"

    def local_install(self) -> dict:
        return aor_install.install_local(self.source, self.home, self.bin_dir)

    def context(self) -> dict:
        root = (self.home / "current").resolve()
        return {
            "kind": "managed", "root": root, "home": self.home,
            "version": json.loads((root / "agent-manifest.json").read_text())["version"],
            "commit": git(root, "rev-parse", "HEAD"), "channel": "stable",
            "repository": aor_install.OFFICIAL_REPOSITORY, "current_path": self.home / "current",
        }

    def latest(self, version: str) -> dict:
        return {"status": "update_available", "latest_version": version, "tag_name": f"v{version}"}

    def release_checkout(self, source: Path):
        def checkout(tag: str, destination: Path) -> str:
            self.assertEqual(tag, f"v{json.loads((source / 'agent-manifest.json').read_text())['version']}")
            return aor_install._checkout_local(source, destination)
        return checkout

    def test_local_install_has_independent_snapshot_and_real_launcher(self) -> None:
        source_head = git(self.source, "rev-parse", "HEAD")
        result = self.local_install()
        current = (self.home / "current").resolve()
        self.assertTrue((self.home / "current").is_symlink())
        self.assertNotEqual(current, self.source)
        self.assertEqual(result["version"], "3.2.0")
        self.assertEqual(git(current, "rev-parse", "HEAD"), source_head)
        self.assertEqual(git(self.source, "status", "--porcelain"), "")
        self.assertEqual(git(current, "status", "--porcelain"), "")
        self.assertTrue(os.access(self.bin_dir / "aor", os.X_OK))
        run = subprocess.run([str(self.bin_dir / "aor"), "--version"], capture_output=True, text=True, check=False)
        self.assertEqual(run.returncode, 0, run.stderr)
        metadata = json.loads((self.home / "install.json").read_text())
        self.assertEqual(metadata["repository"], aor_install.OFFICIAL_REPOSITORY)
        self.assertEqual(metadata["manager"], "aor")
        self.assertNotIn("version", metadata)

    def test_default_install_accepts_archive_and_fetches_stable_release(self) -> None:
        archive = self.base / "archive"
        archive.mkdir()
        (archive / "agent-manifest.json").write_bytes((self.source / "agent-manifest.json").read_bytes())
        with patch.object(aor_install, "check_update", return_value=self.latest("3.2.0")) as check, \
             patch.object(aor_install, "_checkout_release", side_effect=self.release_checkout(self.source)):
            result = aor_install.install(archive, self.home, self.bin_dir)
        self.assertEqual(result["version"], "3.2.0")
        self.assertTrue(check.call_args.kwargs["refresh"])

    def test_local_install_rejects_dirty_source_without_creating_installation(self) -> None:
        (self.source / "notes.txt").write_text("未提交修改", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "修改|干净"):
            self.local_install()
        self.assertFalse((self.home / "current").exists())
        self.assertFalse((self.bin_dir / "aor").exists())

    def test_local_install_rejects_wrong_origin(self) -> None:
        git(self.source, "remote", "set-url", "origin", "https://example.invalid/other.git")
        with self.assertRaisesRegex(ValueError, "来源|仓库"):
            self.local_install()

    def test_install_does_not_overwrite_unrelated_executable(self) -> None:
        self.bin_dir.mkdir()
        launcher = self.bin_dir / "aor"
        launcher.write_text("用户已有命令", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "命令|覆盖"):
            self.local_install()
        self.assertEqual(launcher.read_text(), "用户已有命令")
        self.assertFalse((self.home / "current").exists())

    def test_update_switches_pointer_and_preserves_old_snapshot(self) -> None:
        self.local_install()
        old_root = (self.home / "current").resolve()
        next_source = repository(self.base / "next-source", "3.3.0")
        launcher_before = (self.bin_dir / "aor").read_bytes()
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")), \
             patch.object(aor_install, "_checkout_release", side_effect=self.release_checkout(next_source)):
            result = aor_install.update(self.context())
        self.assertEqual(result["status"], "updated")
        self.assertEqual(result["previous_version"], "3.2.0")
        self.assertEqual(result["version"], "3.3.0")
        self.assertNotEqual((self.home / "current").resolve(), old_root)
        self.assertTrue(old_root.is_dir())
        self.assertEqual((self.bin_dir / "aor").read_bytes(), launcher_before)

    def test_update_refuses_untracked_or_modified_current(self) -> None:
        self.local_install()
        old_root = (self.home / "current").resolve()
        (old_root / "private-notes.txt").write_text("本地文件", encoding="utf-8")
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")), \
             patch.object(aor_install, "_checkout_release") as checkout:
            with self.assertRaisesRegex(ValueError, "修改|干净"):
                aor_install.update(self.context())
        checkout.assert_not_called()
        self.assertEqual((self.home / "current").resolve(), old_root)

    def test_failed_candidate_validation_keeps_current_and_launcher(self) -> None:
        self.local_install()
        old_root = (self.home / "current").resolve()
        next_source = repository(self.base / "broken-source", "3.3.0")
        (next_source / "scripts" / "radar.py").write_text("raise SystemExit(2)\n")
        git(next_source, "add", ".")
        git(next_source, "commit", "-qm", "故障入口")
        launcher_before = (self.bin_dir / "aor").read_bytes()
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")), \
             patch.object(aor_install, "_checkout_release", side_effect=self.release_checkout(next_source)):
            with self.assertRaisesRegex(ValueError, "校验|入口"):
                aor_install.update(self.context())
        self.assertEqual((self.home / "current").resolve(), old_root)
        self.assertEqual((self.bin_dir / "aor").read_bytes(), launcher_before)

    def test_release_tag_cannot_inject_another_ref(self) -> None:
        self.local_install()
        response = {"status": "update_available", "latest_version": "3.3.0", "tag_name": "--upload-pack=bad"}
        with patch.object(aor_install, "check_update", return_value=response), \
             patch.object(aor_install, "_checkout_release") as checkout:
            with self.assertRaisesRegex(ValueError, "标签|版本"):
                aor_install.update(self.context())
        checkout.assert_not_called()

    def test_same_version_does_not_replace_current(self) -> None:
        self.local_install()
        with patch.object(aor_install, "check_update", return_value=self.latest("3.2.0")), \
             patch.object(aor_install, "_checkout_release") as checkout:
            result = aor_install.update(self.context())
        self.assertEqual(result["status"], "up_to_date")
        checkout.assert_not_called()

    def test_newer_local_version_is_reported_ahead_without_downgrade(self) -> None:
        self.local_install()
        with patch.object(aor_install, "check_update", return_value=self.latest("3.1.0")), \
             patch.object(aor_install, "_checkout_release") as checkout:
            result = aor_install.update(self.context())
        self.assertEqual(result["status"], "ahead")
        self.assertFalse(result["reload_required"])
        checkout.assert_not_called()

    def test_candidate_doctor_failure_keeps_previous_snapshot(self) -> None:
        self.local_install()
        previous = (self.home / "current").resolve()
        source = repository(self.base / "bad-doctor", "3.3.0")
        (source / "scripts" / "radar.py").write_text(
            "import sys\nprint('{\"health\": \"error\"}' if 'doctor' in sys.argv else 'help')\n")
        git(source, "add", ".")
        git(source, "commit", "-qm", "损坏的诊断入口")
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")), \
             patch.object(aor_install, "_checkout_release", side_effect=self.release_checkout(source)):
            with self.assertRaisesRegex(ValueError, "校验|诊断"):
                aor_install.update(self.context())
        self.assertEqual((self.home / "current").resolve(), previous)

    def test_launcher_json_update_does_not_hold_reader_lock(self) -> None:
        self.local_install()
        current = (self.home / "current").resolve()
        (current / "bin" / "aor").write_text(
            "import fcntl, os\nfrom pathlib import Path\n"
            "with (Path(os.environ['AOR_INSTALL_HOME'])/'update.lock').open('a+') as handle:\n"
            "    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n")
        result = subprocess.run([str(self.bin_dir / "aor"), "--json", "update"],
                                capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_rejects_symlink_lock_without_touching_target(self) -> None:
        self.local_install()
        target = self.base / "other-file"
        target.write_text("保留", encoding="utf-8")
        (self.home / "update.lock").unlink()
        (self.home / "update.lock").symlink_to(target)
        result = subprocess.run([str(self.bin_dir / "aor"), "--version"], capture_output=True, text=True, timeout=10)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(target.read_text(), "保留")

    def test_update_rejects_unmanaged_source_checkout(self) -> None:
        with self.assertRaisesRegex(ValueError, "受管|安装"):
            aor_install.update({"kind": "source", "root": self.source})

    def test_no_stable_release_cannot_be_silently_installed(self) -> None:
        with patch.object(aor_install, "check_update", return_value={"status": "unreleased"}), \
             patch.object(aor_install, "_checkout_release") as checkout:
            with self.assertRaisesRegex(ValueError, "稳定|发行|Release"):
                aor_install.install(self.source, self.home, self.bin_dir)
        checkout.assert_not_called()

    def test_active_run_lock_prevents_update_before_network(self) -> None:
        self.local_install()
        with aor_install.lock(self.home, exclusive=False, blocking=False), \
             patch.object(aor_install, "check_update") as check:
            with self.assertRaisesRegex(ValueError, "使用|任务|重试"):
                aor_install.update(self.context())
        check.assert_not_called()

    def test_ignored_credentials_are_protected_while_python_cache_is_allowed(self) -> None:
        self.local_install()
        current = (self.home / "current").resolve()
        (current / "__pycache__").mkdir()
        (current / "__pycache__" / "example.pyc").write_bytes(b"cache")
        aor_install._assert_clean(current)
        (current / ".git" / "info" / "exclude").write_text(".env\n")
        (current / ".env").write_text("PRIVATE_TEST_VALUE=example\n")
        with self.assertRaisesRegex(ValueError, "修改|干净"):
            aor_install._assert_clean(current)

    def test_release_checkout_fetches_only_exact_tag_from_official_remote(self) -> None:
        with patch.object(aor_install, "_git", return_value="a" * 40) as execute:
            aor_install._checkout_release("v3.2.0", self.base / "download")
        calls = [call.args[1:] for call in execute.call_args_list]
        self.assertIn(("remote", "add", "origin", aor_install.OFFICIAL_REPOSITORY), calls)
        self.assertIn(("fetch", "--depth=1", "--no-tags", "origin", "refs/tags/v3.2.0"), calls)

    def test_launcher_publication_failure_rolls_back_current(self) -> None:
        self.local_install()
        old_root = (self.home / "current").resolve()
        launcher_before = (self.bin_dir / "aor").read_bytes()
        next_source = repository(self.base / "next-source", "3.3.0")
        real_replace = os.replace

        def fail_launcher(source, destination):
            if Path(destination) == self.bin_dir / "aor":
                raise OSError("测试命令发布失败")
            return real_replace(source, destination)

        with patch.object(aor_install.os, "replace", side_effect=fail_launcher):
            with self.assertRaises(OSError):
                aor_install.install_local(next_source, self.home, self.bin_dir)
        self.assertEqual((self.home / "current").resolve(), old_root)
        self.assertEqual((self.bin_dir / "aor").read_bytes(), launcher_before)

    def test_failed_first_launcher_publication_does_not_leave_active_installation(self) -> None:
        real_replace = os.replace

        def fail_launcher(source, destination):
            if Path(destination) == self.bin_dir / "aor":
                raise OSError("测试命令发布失败")
            return real_replace(source, destination)

        with patch.object(aor_install.os, "replace", side_effect=fail_launcher):
            with self.assertRaises(OSError):
                self.local_install()
        self.assertFalse(os.path.lexists(self.home / "current"))
        self.assertFalse((self.home / "install.json").exists())
        self.assertFalse((self.bin_dir / "aor").exists())

    def test_external_current_pointer_cannot_be_updated(self) -> None:
        self.local_install()
        context = self.context()
        (self.home / "current").unlink()
        (self.home / "current").symlink_to(self.source, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "受管|目录"):
            aor_install.update(context)
        self.assertEqual((self.home / "current").resolve(), self.source)

    def test_stale_context_is_reloaded_under_update_lock(self) -> None:
        self.local_install()
        stale = self.context()
        next_source = repository(self.base / "next-source", "3.3.0")
        aor_install.install_local(next_source, self.home, self.bin_dir)
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")) as check, \
             patch.object(aor_install, "_checkout_release") as checkout:
            result = aor_install.update(stale)
        self.assertEqual(check.call_args.args[0]["version"], "3.3.0")
        self.assertEqual(result["status"], "up_to_date")
        checkout.assert_not_called()

    def test_tag_version_mismatch_keeps_old_version(self) -> None:
        self.local_install()
        old_root = (self.home / "current").resolve()
        with patch.object(aor_install, "check_update", return_value=self.latest("3.3.0")), \
             patch.object(aor_install, "_checkout_release", side_effect=lambda tag, path: aor_install._checkout_local(self.source, path)):
            with self.assertRaisesRegex(ValueError, "标签|版本"):
                aor_install.update(self.context())
        self.assertEqual((self.home / "current").resolve(), old_root)


if __name__ == "__main__":
    unittest.main()
