#!/usr/bin/env python3
"""从固定 Git 提交构建可复现的 AOR 稳定版本发行包。"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

PROJECT_NAME = "ai-opportunity-radar"
REPOSITORY = "https://github.com/Snychng/ai-opportunity-radar.git"
SEMVER = re.compile(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)")
IGNORED_PARTS = {"__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".git"}
REQUIRED_FILES = {"agent-manifest.json", "SKILL.md", "scripts/radar.py", "bin/aor"}


class ReleaseError(ValueError):
    """发行输入或归档内容不符合要求。"""


def _git(root: Path, *arguments: str) -> bytes:
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "GIT_ATTR_NOSYSTEM": "1",
                        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
             "-c", "core.attributesFile=/dev/null", "-c", "tar.umask=0022", *arguments],
            capture_output=True, check=False, timeout=60,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError("无法执行发行所需的 Git 读取") from exc
    if result.returncode:
        raise ReleaseError("Git 引用不存在，或无法读取其提交内容")
    return result.stdout


def _version(root: Path, commit: str, ref: str) -> str:
    try:
        manifest = json.loads(_git(root, "show", f"{commit}:agent-manifest.json"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ReleaseError("提交中的项目清单不是有效 JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("name") != PROJECT_NAME:
        raise ReleaseError("提交中的项目身份不匹配")
    version = manifest.get("version")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ReleaseError("清单版本必须是完整的主版本.次版本.修订版本")
    tag = ref.removeprefix("refs/tags/")
    if ref.startswith("refs/tags/") or re.match(r"^v[0-9]", ref):
        if not tag.startswith("v") or not SEMVER.fullmatch(tag[1:]):
            raise ReleaseError("发行标签必须是 v主版本.次版本.修订版本")
        if tag != f"v{version}":
            raise ReleaseError("发行标签与清单版本不匹配")
    return version


def _committed_archive(root: Path, commit: str) -> bytes:
    objects_path = Path(os.fsdecode(_git(root, "rev-parse", "--git-path", "objects").rstrip(b"\n")))
    objects_path = (root / objects_path).resolve()
    if "\n" in str(objects_path) or "\r" in str(objects_path):
        raise ReleaseError("Git 对象目录不能包含换行符")
    # 独立的临时裸仓库只读取原仓库对象，排除本地 info/attributes 对发行内容的影响。
    with tempfile.TemporaryDirectory(prefix="aor-release-source-") as temporary:
        bare = Path(temporary)
        template = bare / "empty-template"
        template.mkdir()
        object_options = ["--object-format=sha256"] if len(commit) == 64 else []
        _git(bare, "init", "--bare", "--quiet", f"--template={template}", *object_options)
        (bare / "objects" / "info" / "alternates").write_text(str(objects_path) + "\n", encoding="utf-8")
        return _git(bare, "archive", "--format=tar", f"--prefix={PROJECT_NAME}/", commit)


def _archive(root: Path, commit: str) -> bytes:
    raw = _committed_archive(root, commit)
    destination = io.BytesIO()
    seen: set[str] = set()
    files: set[str] = set()
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:") as original:
            with tarfile.open(fileobj=destination, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for entry in original:
                    path = PurePosixPath(entry.name)
                    if path.is_absolute() or ".." in path.parts or "\\" in entry.name or not path.parts:
                        raise ReleaseError("归档成员路径不安全")
                    if path.parts[0] != PROJECT_NAME or entry.name in seen:
                        raise ReleaseError("归档成员不在项目根目录内，或路径重复")
                    seen.add(entry.name)
                    if entry.issym() or entry.islnk():
                        raise ReleaseError("发行包不允许包含链接")
                    if not entry.isfile() and not entry.isdir():
                        raise ReleaseError("发行包只能包含普通文件和目录")
                    if any(part in IGNORED_PARTS for part in path.parts[1:]):
                        continue
                    if path.suffix in {".pyc", ".pyo"} or path.name == ".DS_Store":
                        continue
                    # 固定所有者和扩展元数据，使归档不受构建机器用户信息影响。
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.pax_headers = {}
                    entry.mode = 0o755 if entry.isdir() or entry.mode & 0o111 else 0o644
                    if entry.isfile():
                        relative = path.relative_to(PROJECT_NAME).as_posix()
                        files.add(relative)
                        if relative == "bin/aor" and not entry.mode & 0o111:
                            raise ReleaseError("bin/aor 必须在 Git 提交中具有可执行权限")
                        archive.addfile(entry, original.extractfile(entry))
                    else:
                        archive.addfile(entry)
    except (tarfile.TarError, OSError) as exc:
        raise ReleaseError("无法读取安全的源码归档") from exc
    missing = REQUIRED_FILES - files
    if missing:
        raise ReleaseError("归档缺少必要入口：" + ", ".join(sorted(missing)))
    compressed = io.BytesIO()
    with gzip.GzipFile(filename="", fileobj=compressed, mode="wb", mtime=0, compresslevel=9) as archive:
        archive.write(destination.getvalue())
    return compressed.getvalue()


def _write(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def build_release(root: Path, ref: str, output_dir: Path) -> dict[str, Any]:
    """读取 ref 对应的提交，返回并落盘版本清单、压缩包与校验和。"""
    if not isinstance(ref, str) or not ref or ref.startswith("-") or "\x00" in ref:
        raise ReleaseError("Git 引用无效")
    root = Path(root).resolve()
    commit = _git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        raise ReleaseError("Git 引用没有解析为精确提交")
    version = _version(root, commit, ref)
    content = _archive(root, commit)
    archive_name = f"aor-v{version}.tar.gz"
    manifest = {
        "version": version, "tag": f"v{version}", "commit": commit,
        "archive": archive_name, "sha256": hashlib.sha256(content).hexdigest(), "repository": REPOSITORY,
    }
    manifest_bytes = (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()
    checksums = (f"{manifest['sha256']}  {archive_name}\n"
                 f"{hashlib.sha256(manifest_bytes).hexdigest()}  release-manifest.json\n").encode()
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _write(output_dir / archive_name, content)
    _write(output_dir / "release-manifest.json", manifest_bytes)
    _write(output_dir / "SHA256SUMS", checksums)
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="从精确 Git 提交构建可复现的 AOR 发行包")
    parser.add_argument("--ref", default="HEAD", help="提交、分支或稳定版本标签，默认 HEAD")
    parser.add_argument("--output-dir", type=Path, required=True, help="发行包输出目录")
    arguments = parser.parse_args(argv)
    try:
        result = build_release(Path(__file__).resolve().parents[1], arguments.ref, arguments.output_dir)
    except (ReleaseError, OSError) as exc:
        print(f"发行包构建失败：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
