#!/usr/bin/env python3
"""AOR 的安装身份、路径和运行锁；研究数据与安装状态相互独立。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OFFICIAL_REPOSITORY = "https://github.com/Snychng/ai-opportunity-radar.git"
MANAGED_HOME = Path(os.environ.get("AOR_INSTALL_HOME", "~/.local/share/aor")).expanduser()
CACHE_HOME = Path(os.environ.get("AOR_CACHE_HOME", "~/.cache/aor")).expanduser()
SEMVER_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
_preflight_done = False


class AorError(ValueError):
    """安装、运行或更新条件不满足。"""


def enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}


def process_environment(**overrides: str) -> dict[str, str]:
    """清除调用方的 Git 仓库定位和配置覆盖，保留代理及研究设置。"""
    return {**{key: value for key, value in os.environ.items() if not key.startswith("GIT_")}, **overrides}


def read_manifest(root: Path) -> dict[str, Any]:
    root = Path(root).resolve()
    try:
        payload = json.loads((root / "agent-manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AorError("无法读取有效的 agent-manifest.json") from exc
    if not isinstance(payload, dict) or payload.get("name") != "ai-opportunity-radar":
        raise AorError("安装目录不是 AI Opportunity Radar")
    if not isinstance(payload.get("version"), str) or not SEMVER_RE.fullmatch(payload["version"]):
        raise AorError("版本必须是完整的主版本.次版本.修订版本")
    skills = payload.get("skills", [{"name": payload["name"], "path": "."}])
    if not isinstance(skills, list) or not skills:
        raise AorError("skills 必须是非空数组")
    names = set()
    for skill in skills:
        if not isinstance(skill, dict) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", str(skill.get("name", ""))):
            raise AorError("技能登记缺少有效名称")
        if skill["name"] in names:
            raise AorError("技能名称重复")
        names.add(skill["name"])
        relative = skill.get("path")
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
            raise AorError("技能路径必须是安装目录内的相对路径")
        try:
            target = (root / relative).resolve()
        except (OSError, RuntimeError) as error:
            raise AorError("技能路径无法解析，可能包含损坏的符号链接") from error
        if not target.is_relative_to(root):
            raise AorError("技能路径越出安装目录")
    return {**payload, "skills": skills}


def _git(root: Path, *arguments: str) -> str | None:
    try:
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false",
                                 "-C", str(root), *arguments], capture_output=True, text=True,
                                env=process_environment(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0"),
                                timeout=2, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def official_origin(value: str | None) -> bool:
    return isinstance(value, str) and value.rstrip("/").lower() in {
        OFFICIAL_REPOSITORY.lower(), OFFICIAL_REPOSITORY.removesuffix(".git").lower(),
        "git@github.com:snychng/ai-opportunity-radar.git", "ssh://git@github.com/snychng/ai-opportunity-radar.git",
    }


def installation_context(root: Path | None = None) -> dict[str, Any]:
    root = Path(root or PROJECT_ROOT).resolve()
    try:
        manifest = read_manifest(root)
        version, manifest_error = manifest["version"], None
    except AorError as exc:
        version, manifest_error = None, str(exc)
    git_root = _git(root, "rev-parse", "--show-toplevel")
    exact_git = bool(git_root and Path(git_root).resolve() == root)
    commit = _git(root, "rev-parse", "HEAD") if exact_git else None
    origin_ok = official_origin(_git(root, "remote", "get-url", "origin")) if exact_git else False
    changes = _git(root, "status", "--porcelain", "--untracked-files=all") if exact_git else None
    context: dict[str, Any] = {
        "kind": "source" if exact_git and origin_ok else "unmanaged",
        "root": root, "home": None, "version": version, "commit": commit,
        "repository": OFFICIAL_REPOSITORY, "channel": "stable", "current_path": None,
        "origin_verified": origin_ok, "manifest_error": manifest_error,
        "worktree_clean": changes == "" if changes is not None else None,
    }
    if root.parent.name != "versions":
        return context
    home = root.parent.parent
    try:
        metadata = json.loads((home / "install.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return context
    if (not isinstance(metadata, dict) or metadata.get("schema_version") != 1
            or metadata.get("manager") != "aor" or metadata.get("channel") != "stable"
            or metadata.get("repository") != OFFICIAL_REPOSITORY):
        return context
    current = home / "current"
    if not current.is_symlink() or current.resolve().parent != root.parent:
        return context
    context.update(kind="managed", home=home, current_path=current, bin_path=metadata.get("bin_path"),
                   manager="aor", is_current=current.resolve() == root)
    return context


def atomic_json(path: Path, payload: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}-", delete=False) as handle:
            temporary = handle.name
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


@contextmanager
def lock(home: Path, exclusive: bool = False, blocking: bool = False) -> Iterator[None]:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(home / "update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise AorError("更新锁不是普通文件")
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        try:
            fcntl.flock(fd, operation | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise AorError("AOR 正在使用或更新，请在当前任务结束后重试") from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def update_notice(result: dict[str, Any]) -> str | None:
    """仅为有效的新稳定版本生成提示，其余检查结果保持静默。"""
    version = result.get("latest_version")
    if (result.get("status") != "update_available" or not isinstance(version, str)
            or not version.isascii() or not SEMVER_RE.fullmatch(version)):
        return None
    return f"AOR 发现新版本 v{version}，可运行 `aor update` 更新。更新后请重新读取技能说明。"


def preflight(context: dict[str, Any] | None = None) -> None:
    """首次业务调用仅检查版本，失败不影响研究结果或标准输出。"""
    global _preflight_done
    if _preflight_done or enabled("AOR_NO_UPDATE_CHECK"):
        return
    _preflight_done = True
    if any(arg in {"--help", "-h", "--version"} for arg in sys.argv[1:]):
        return
    try:
        from aor_status import check_update

        result = check_update(context or installation_context(), offline=enabled("AOR_OFFLINE"))
        notice = update_notice(result)
        if notice is not None:
            print(notice, file=sys.stderr)
    except Exception:
        # 在线诊断的错误在 doctor 中展示，不能改写正常业务命令的结果或退出码。
        return


def run_legacy(main: Callable[[], int], script: str) -> int:
    """兼容独立脚本入口，同时持有对应安装的运行锁。"""
    context = installation_context(Path(script).resolve().parents[1])
    if context["kind"] == "managed":
        try:
            with lock(context["home"]):
                preflight(context)
                return main()
        except (AorError, OSError) as error:
            print(f"AOR：{error}", file=sys.stderr)
            return 1
    preflight(context)
    return main()
