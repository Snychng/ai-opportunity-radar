#!/usr/bin/env python3
"""安装并更新 AOR 自己管理的稳定版本，保留开发目录与旧版本。"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from aor_runtime import MANAGED_HOME, OFFICIAL_REPOSITORY, PROJECT_ROOT, atomic_json, lock, read_manifest
from aor_status import check_update

VERSION_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
LAUNCHER_MARKER = "# AOR managed launcher v1"
REQUIRED_FILES = ("agent-manifest.json", "SKILL.md", "scripts/radar.py", "bin/aor",
                  "scripts/aor_runtime.py", "scripts/aor_status.py", "scripts/aor_install.py")


def _version(value: object) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION_PATTERN.fullmatch(value):
        raise ValueError("稳定版本必须使用 major.minor.patch 格式。")
    return tuple(int(part) for part in value.split("."))


def _identity(root: Path) -> dict:
    manifest = read_manifest(root)
    if manifest.get("name") != "ai-opportunity-radar":
        raise ValueError("来源目录不是 AI Opportunity Radar 项目。")
    _version(manifest.get("version"))
    return manifest


def _git(root: Path, *arguments: str, timeout: int = 120) -> str:
    environment = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", *arguments],
            cwd=root, env=environment, capture_output=True, text=True, check=False, timeout=timeout,
        )
    except FileNotFoundError as error:
        raise ValueError("安装和更新需要 Git，请先安装 Git。") from error
    except subprocess.TimeoutExpired as error:
        raise ValueError("Git 操作超时，现有版本未切换。") from error
    if result.returncode:
        raise ValueError(f"Git 操作失败（{arguments[0]}），请检查仓库、网络与目录权限。")
    return result.stdout.strip()


def _official_origin(value: str) -> bool:
    return value.rstrip("/").lower() in {
        OFFICIAL_REPOSITORY.lower(), OFFICIAL_REPOSITORY.removesuffix(".git").lower(),
        "git@github.com:snychng/ai-opportunity-radar.git",
        "ssh://git@github.com/snychng/ai-opportunity-radar.git",
    }


def _assert_source(root: Path) -> None:
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root.resolve():
        raise ValueError("来源必须是完整的项目 Git 根目录。")
    if not _official_origin(_git(root, "remote", "get-url", "origin")):
        raise ValueError("Git 来源仓库与 AOR 官方仓库不一致。")
    _assert_clean(root)


def _cache_path(value: str) -> bool:
    path = Path(value)
    return (
        bool({"__pycache__", ".pytest_cache", ".ruff_cache"}.intersection(path.parts))
        or path.suffix in {".pyc", ".pyo"}
        or path.name == ".DS_Store"
    )


def _assert_clean(root: Path) -> None:
    changes = _git(root, "status", "--porcelain=v1", "--untracked-files=all", "--ignored=matching", "-z")
    for item in changes.split("\0"):
        if not item:
            continue
        # 被忽略的凭证、笔记等也需要保护，只排除已知运行缓存。
        if item.startswith("!! ") and _cache_path(item[3:]):
            continue
        raise ValueError("版本目录存在本地修改或未跟踪文件，请先保存并整理为干净工作区。")


def _checkout_local(source: Path, destination: Path) -> str:
    expected = _git(source, "rev-parse", "HEAD")
    _git(destination.parent, "clone", "--no-hardlinks", "--dissociate", "--no-checkout", "--", str(source), str(destination))
    _git(destination, "remote", "set-url", "origin", OFFICIAL_REPOSITORY)
    _git(destination, "checkout", "--detach", expected)
    return _git(destination, "rev-parse", "HEAD")


def _checkout_release(tag: str, destination: Path) -> str:
    if not tag.startswith("v"):
        raise ValueError("稳定发行标签必须以 v 开头。")
    _version(tag[1:])
    destination.mkdir()
    _git(destination, "init", "-q")
    _git(destination, "remote", "add", "origin", OFFICIAL_REPOSITORY)
    _git(destination, "fetch", "--depth=1", "--no-tags", "origin", f"refs/tags/{tag}")
    _git(destination, "checkout", "--detach", "FETCH_HEAD")
    return _git(destination, "rev-parse", "HEAD")


def _validate_candidate(root: Path, expected_version: str) -> str:
    manifest = _identity(root)
    if manifest["version"] != expected_version:
        raise ValueError("发行标签与技能 manifest 版本不一致，未切换版本。")
    for name in REQUIRED_FILES:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"候选版本校验失败，缺少必要文件：{name}")
        _git(root, "ls-files", "--error-unmatch", "--", name)
    for skill in manifest["skills"]:
        instructions = root / skill["path"] / "SKILL.md"
        if not instructions.is_file() or instructions.is_symlink():
            raise ValueError("候选版本缺少已登记技能的 SKILL.md。")
    _assert_source(root)
    environment = dict(os.environ, AOR_OFFLINE="1", AOR_NO_UPDATE_CHECK="1", PYTHONDONTWRITEBYTECODE="1")
    try:
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "radar.py"), "--help"],
            cwd=root, env=environment, capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("候选版本离线入口校验失败，未切换版本。") from error
    if result.returncode:
        raise ValueError("候选版本离线入口校验失败，未切换版本。")
    try:
        result = subprocess.run(
            [sys.executable, str(root / "scripts" / "radar.py"), "doctor", "--offline", "--json"],
            cwd=root, env=environment, capture_output=True, text=True, timeout=15, check=False,
        )
        health = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        raise ValueError("候选版本离线诊断校验失败，未切换版本。") from error
    if result.returncode or not isinstance(health, dict) or health.get("health") not in {"ok", "warning"}:
        raise ValueError("候选版本离线诊断校验失败，未切换版本。")
    _assert_clean(root)
    commit = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40,64}", commit):
        raise ValueError("无法确定候选版本的 Git 提交。")
    return commit


def _latest(context: dict) -> tuple[str, str]:
    result = check_update(context, refresh=True, offline=False)
    if result.get("status") not in {"update_available", "available", "up_to_date", "current", "ahead"}:
        raise ValueError("无法确认最新稳定 Release，请稍后重试；现有版本保持不变。")
    version = result.get("latest_version")
    tag = result.get("tag_name")
    _version(version)
    if tag != f"v{version}":
        raise ValueError("稳定发行标签与版本不一致。")
    return tag, version


def _paths(source: Path, home: Path | None, bin_dir: Path | None) -> tuple[Path, Path]:
    install_home = Path(home or MANAGED_HOME).expanduser().resolve()
    executable_dir = Path(bin_dir or Path.home() / ".local" / "bin").expanduser().resolve()
    if source == install_home or source in install_home.parents or source == executable_dir or source in executable_dir.parents:
        raise ValueError("受管安装和命令目录必须位于源码目录之外。")
    return install_home, executable_dir / "aor"


def _launcher_text(home: Path) -> str:
    return f'''#!/usr/bin/env python3
"""启动已登记的 AOR 版本。"""
{LAUNCHER_MARKER}
import fcntl
import os
import stat
import subprocess
import sys
from pathlib import Path

INSTALL_HOME = {str(home)!r}
home = Path(INSTALL_HOME)
environment = dict(os.environ, AOR_INSTALL_HOME=INSTALL_HOME, PYTHONDONTWRITEBYTECODE="1")
arguments = [argument for argument in sys.argv[1:] if argument != "--json"]
exclusive_command = bool(arguments) and arguments[0] in {{"update", "install"}}
try:
    descriptor = os.open(home / "update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "a+") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError("更新锁不是普通文件。")
        if not exclusive_command:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("AOR 正在更新，请稍后重试。") from error
        current = (home / "current").resolve(strict=True)
        if current.parent != (home / "versions").resolve():
            raise ValueError("当前版本不在受管目录中。")
        code = subprocess.call([sys.executable, str(current / "bin" / "aor"), *sys.argv[1:]], env=environment)
        raise SystemExit(code)
except KeyboardInterrupt:
    raise SystemExit(130)
except (OSError, ValueError) as error:
    print(f"AOR 启动失败：{{error}}", file=sys.stderr)
    raise SystemExit(1)
'''


def _check_launcher(path: Path, home: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_symlink() or not path.is_file():
        raise ValueError("aor 命令路径已被其他文件占用，不会覆盖。")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("已有 aor 命令不属于本次安装，不会覆盖。") from error
    if LAUNCHER_MARKER not in text or f"INSTALL_HOME = {str(home)!r}\n" not in text:
        raise ValueError("已有 aor 命令不属于本次安装，不会覆盖。")


def _registered(home: Path) -> dict | None:
    path = home / "install.json"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("安装记录必须是受管目录中的普通文件。")
    if not path.exists():
        if os.path.lexists(home / "current"):
            raise ValueError("已有 current 未登记为 AOR 受管安装，不会覆盖。")
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("安装记录无法读取，请先检查受管目录。") from error
    if not isinstance(record, dict) or record.get("schema_version") != 1 or record.get("manager") != "aor" \
            or record.get("repository") != OFFICIAL_REPOSITORY or record.get("channel") != "stable":
        raise ValueError("安装来源或管理器不匹配，不能更新此目录。")
    return record


def _current(home: Path) -> Path:
    if (home / "versions").is_symlink():
        raise ValueError("受管版本目录不能指向其他位置。")
    path = home / "current"
    if not path.is_symlink():
        raise ValueError("受管安装缺少 current 版本指针。")
    try:
        root = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("受管版本指针损坏，无法更新。") from error
    if root.parent != (home / "versions").resolve() or not root.is_dir():
        raise ValueError("当前版本不在本次受管安装目录中。")
    return root


def _switch(home: Path, root: Path) -> None:
    link = home / f".current-{uuid.uuid4().hex}"
    try:
        link.symlink_to(root.relative_to(home), target_is_directory=True)
        os.replace(link, home / "current")
    finally:
        if os.path.lexists(link):
            link.unlink()


def _result(status: str, home: Path, root: Path, bin_path: Path, **extra: object) -> dict:
    return {
        "status": status, "version": _identity(root)["version"], "commit": _git(root, "rev-parse", "HEAD"),
        "root": str(root), "current_path": str(home / "current"), "bin_path": str(bin_path),
        "reload_required": status in {"installed", "updated"},
        "instructions": str(home / "current" / "SKILL.md"), **extra,
    }


def _candidate(home: Path, tag: str, version: str, local_source: Path | None = None) -> Path:
    versions = home / "versions"
    if versions.is_symlink():
        raise ValueError("受管版本目录不能指向其他位置。")
    versions.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".staging-", dir=versions) as temporary:
        staging = Path(temporary) / "checkout"
        if local_source is None:
            _checkout_release(tag, staging)
        else:
            _checkout_local(local_source, staging)
        commit = _validate_candidate(staging, version)
        destination = versions / f"{tag}-{commit[:12]}"
        if os.path.lexists(destination):
            if destination.is_symlink() or not destination.is_dir():
                raise ValueError("候选版本目录已被其他文件占用。")
            existing_commit = _validate_candidate(destination, version)
            if existing_commit != commit:
                raise ValueError("候选版本目录与目标提交不一致。")
        else:
            staging.rename(destination)
    return destination


def _install(
    source_root: Path, home: Path | None, bin_dir: Path | None, *, local: bool,
) -> dict:
    source = Path(source_root).expanduser().resolve()
    manifest = _identity(source)
    if local:
        _assert_source(source)
    install_home, bin_path = _paths(source, home, bin_dir)
    _check_launcher(bin_path, install_home)
    install_home.mkdir(parents=True, exist_ok=True)
    with lock(install_home, exclusive=True, blocking=False):
        registered = _registered(install_home)
        if registered is not None and registered.get("bin_path") != str(bin_path):
            raise ValueError("此安装已登记其他命令位置，不会隐式迁移。")
        old_root = _current(install_home) if registered else None
        if old_root is not None:
            _assert_source(old_root)
        context = {
            "kind": "source", "root": str(source), "version": manifest["version"],
            "repository": OFFICIAL_REPOSITORY, "channel": "stable",
        }
        tag, version = (f"v{manifest['version']}", manifest["version"]) if local else _latest(context)
        candidate = _candidate(install_home, tag, version, source if local else None)
        result = _result("installed", install_home, candidate, bin_path, source_kind="local" if local else "release")
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        _check_launcher(bin_path, install_home)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".aor-", dir=bin_path.parent)
        temporary_launcher = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(_launcher_text(install_home))
                output.flush()
                os.fsync(output.fileno())
            temporary_launcher.chmod(0o755)
            if registered is None:
                atomic_json(install_home / "install.json", {
                    "schema_version": 1, "repository": OFFICIAL_REPOSITORY, "channel": "stable", "manager": "aor",
                    "bin_path": str(bin_path), "created_at": datetime.now(timezone.utc).isoformat(),
                })
            try:
                _switch(install_home, candidate)
                os.replace(temporary_launcher, bin_path)
            except OSError:
                if old_root is not None:
                    _switch(install_home, old_root)
                else:
                    (install_home / "current").unlink(missing_ok=True)
                    (install_home / "install.json").unlink(missing_ok=True)
                raise
        finally:
            temporary_launcher.unlink(missing_ok=True)
        return result


def install(source_root: Path, home: Path | None = None, bin_dir: Path | None = None) -> dict:
    """从官方最新稳定 Release 安装，不要求调用目录包含 Git 元数据。"""
    return _install(source_root, home, bin_dir, local=False)


def install_local(source_root: Path, home: Path | None = None, bin_dir: Path | None = None) -> dict:
    """显式将干净且来源匹配的本地 Git 提交安装为独立副本。"""
    return _install(source_root, home, bin_dir, local=True)


def update(context: dict) -> dict:
    """重新核对安装实例，在独占锁内准备并原子切换到最新稳定版本。"""
    if context.get("kind") != "managed" or not context.get("home"):
        raise ValueError("当前是源码或未受管副本，请先运行安装命令创建 AOR 受管安装。")
    home = Path(context["home"]).expanduser().resolve()
    with lock(home, exclusive=True, blocking=False):
        registered = _registered(home)
        if registered is None:
            raise ValueError("此目录未登记为 AOR 受管安装。")
        current = _current(home)
        manifest = _identity(current)
        _assert_source(current)
        bin_path = Path(registered.get("bin_path", ""))
        if not bin_path.is_absolute():
            raise ValueError("安装记录中的命令位置无效。")
        _check_launcher(bin_path, home)
        fresh_context = dict(context, root=str(current), version=manifest["version"], commit=_git(current, "rev-parse", "HEAD"))
        tag, version = _latest(fresh_context)
        if _version(version) <= _version(manifest["version"]):
            status = "up_to_date" if version == manifest["version"] else "ahead"
            return _result(status, home, current, bin_path, latest_version=version)
        candidate = _candidate(home, tag, version)
        _assert_clean(current)
        result = _result("updated", home, candidate, bin_path, previous_version=manifest["version"])
        _switch(home, candidate)
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 AOR 独立受管副本与真实 aor 命令。默认使用官方最新稳定 Release。")
    parser.add_argument("--source", type=Path, help="显式离线安装此干净本地 Git 版本，不查询远端")
    parser.add_argument("--home", type=Path, help="受管版本目录，默认 ~/.local/share/aor")
    parser.add_argument("--bin-dir", type=Path, help="命令目录，默认 ~/.local/bin")
    args = parser.parse_args(argv)
    try:
        result = install_local(args.source, args.home, args.bin_dir) if args.source else install(PROJECT_ROOT, args.home, args.bin_dir)
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "error", "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
