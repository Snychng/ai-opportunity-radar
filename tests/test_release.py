from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from build_release import ReleaseError, build_release


class ReleaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        self.git("init", "--quiet")
        self.git("config", "user.email", "release@example.invalid")
        self.git("config", "user.name", "Release Test")
        self.manifest = {"name": "ai-opportunity-radar", "version": "3.2.0", "instructions": "SKILL.md"}
        (self.repo / "agent-manifest.json").write_text(json.dumps(self.manifest))
        (self.repo / "SKILL.md").write_text("# 测试技能\n")
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "radar.py").write_text("print('测试入口')\n")
        (self.repo / "bin").mkdir()
        (self.repo / "bin" / "aor").write_text("#!/usr/bin/env python3\nprint('测试入口')\n")
        (self.repo / "bin" / "aor").chmod(0o755)
        self.commit()

    def git(self, *args: str) -> str:
        result = subprocess.run(["git", "-C", str(self.repo), *args], capture_output=True, text=True,
                                check=True, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"})
        return result.stdout.strip()

    def commit(self) -> None:
        self.git("add", ".")
        self.git("commit", "--quiet", "-m", "测试快照")

    def test_deterministic_archive_checksums_and_executable_entrypoint(self) -> None:
        self.git("tag", "v3.2.0")
        first = build_release(self.repo, "v3.2.0", self.base / "one")
        second = build_release(self.repo, "v3.2.0", self.base / "two")
        archive = self.base / "one" / "aor-v3.2.0.tar.gz"
        self.assertEqual(archive.read_bytes(), (self.base / "two" / archive.name).read_bytes())
        self.assertEqual(first, second)
        self.assertEqual(first["commit"], self.git("rev-parse", "HEAD"))
        self.assertEqual(first["sha256"], hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertEqual(first["tag"], "v3.2.0")
        self.assertEqual(json.loads((self.base / "one" / "release-manifest.json").read_text()), first)
        for line in (self.base / "one" / "SHA256SUMS").read_text().splitlines():
            digest, name = line.split("  ", 1)
            self.assertEqual(digest, hashlib.sha256((self.base / "one" / name).read_bytes()).hexdigest())
        with tarfile.open(archive) as stream:
            self.assertTrue(stream.getmember("ai-opportunity-radar/bin/aor").mode & 0o111)

    def test_reads_commit_instead_of_worktree_and_excludes_untracked_files(self) -> None:
        (self.repo / "agent-manifest.json").write_text("工作区未提交的无效内容")
        (self.repo / "private-notes.txt").write_text("不要打包")
        result = build_release(self.repo, "HEAD", self.base / "out")
        self.assertEqual(result["version"], "3.2.0")
        with tarfile.open(self.base / "out" / result["archive"]) as stream:
            self.assertNotIn("ai-opportunity-radar/private-notes.txt", stream.getnames())
            source = stream.extractfile("ai-opportunity-radar/agent-manifest.json")
            self.assertIsNotNone(source)
            self.assertEqual(json.load(source), self.manifest)

    def test_excludes_tracked_runtime_caches(self) -> None:
        for relative in ["scripts/__pycache__/module.pyc", ".pytest_cache/state", ".ruff_cache/state", "scripts/module.pyc"]:
            path = self.repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"cache")
        self.commit()
        result = build_release(self.repo, "HEAD", self.base / "out")
        with tarfile.open(self.base / "out" / result["archive"]) as stream:
            self.assertFalse(any("cache" in name or name.endswith(".pyc") for name in stream.getnames()))

    def test_tag_version_must_match_manifest(self) -> None:
        self.git("tag", "v3.1.0")
        with self.assertRaisesRegex(ReleaseError, "版本"):
            build_release(self.repo, "v3.1.0", self.base / "out")
        self.assertFalse((self.base / "out").exists())

    def test_rejects_prerelease_or_invalid_tag(self) -> None:
        for tag in ["v3.2.0-rc.1", "v03.2.0"]:
            self.git("tag", tag)
            with self.subTest(tag=tag), self.assertRaises(ReleaseError):
                build_release(self.repo, tag, self.base / tag)

    def test_rejects_option_injection_and_unknown_ref(self) -> None:
        for reference in ["--output=/tmp/unexpected", "-h", "missing-reference"]:
            with self.subTest(reference=reference), self.assertRaises(ReleaseError):
                build_release(self.repo, reference, self.base / "out")

    def test_rejects_other_project_identity(self) -> None:
        self.manifest["name"] = "unrelated-project"
        (self.repo / "agent-manifest.json").write_text(json.dumps(self.manifest))
        self.commit()
        with self.assertRaisesRegex(ReleaseError, "项目"):
            build_release(self.repo, "HEAD", self.base / "out")

    def test_rejects_symlink_in_archive(self) -> None:
        (self.repo / "external-link").symlink_to("../../external")
        self.commit()
        with self.assertRaisesRegex(ReleaseError, "链接"):
            build_release(self.repo, "HEAD", self.base / "out")

    def test_local_attributes_cannot_change_committed_sources(self) -> None:
        (self.repo / ".git" / "info" / "attributes").write_text("SKILL.md export-ignore\n")
        result = build_release(self.repo, "HEAD", self.base / "out")
        with tarfile.open(self.base / "out" / result["archive"]) as stream:
            self.assertIn("ai-opportunity-radar/SKILL.md", stream.getnames())

    def test_rejects_non_executable_entrypoint(self) -> None:
        self.git("update-index", "--chmod=-x", "bin/aor")
        self.git("commit", "--quiet", "-m", "测试不可执行入口")
        with self.assertRaisesRegex(ReleaseError, "可执行"):
            build_release(self.repo, "HEAD", self.base / "out")

    def test_rejects_archive_path_traversal(self) -> None:
        payload = io.BytesIO()
        with tarfile.open(fileobj=payload, mode="w") as stream:
            entry = tarfile.TarInfo("ai-opportunity-radar/../../escape")
            entry.size = 0
            stream.addfile(entry, io.BytesIO())
        with patch("build_release._committed_archive", return_value=payload.getvalue()):
            with self.assertRaisesRegex(ReleaseError, "路径"):
                build_release(self.repo, "HEAD", self.base / "out")

    def test_annotated_tag_resolves_to_its_commit(self) -> None:
        self.git("tag", "-a", "v3.2.0", "-m", "测试稳定版本")
        result = build_release(self.repo, "refs/tags/v3.2.0", self.base / "out")
        self.assertEqual(result["commit"], self.git("rev-parse", "HEAD"))
        self.assertNotEqual(result["commit"], self.git("rev-parse", "v3.2.0"))


if __name__ == "__main__":
    unittest.main()
