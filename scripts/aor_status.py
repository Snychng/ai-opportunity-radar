"""只诊断本项目安装与技能，并使用有限、无凭证的请求检查稳定发布。"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from aor_runtime import CACHE_HOME, atomic_json, read_manifest

LATEST_RELEASE_URL = "https://api.github.com/repos/Snychng/ai-opportunity-radar/releases/latest"
REPOSITORY_URL = "https://github.com/Snychng/ai-opportunity-radar"
MAX_RESPONSE_BYTES = 1024 * 1024
CACHE_TTL_SECONDS = 24 * 60 * 60
ERROR_CACHE_TTL_SECONDS = 5 * 60
VERSION_PATTERN = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")


def _root(context: dict) -> Path:
    return Path(context["root"]).resolve()


def _manifest(context: dict) -> dict:
    value = read_manifest(_root(context))
    if not isinstance(value, dict) or value.get("name") != "ai-opportunity-radar":
        raise ValueError("项目清单名称无效")
    return value


def get_status(context: dict) -> dict:
    """返回当前加载路径和安装来源；此入口不联网。"""
    try:
        manifest = _manifest(context)
    except (OSError, ValueError, TypeError):
        manifest = {}
    return {
        "name": "ai-opportunity-radar",
        "version": context.get("version") or manifest.get("version"),
        "commit": context.get("commit"),
        "kind": context.get("kind", "unmanaged"),
        "root": str(_root(context)),
        "home": str(context["home"]) if context.get("home") is not None else None,
        "current_path": str(context.get("current_path") or _root(context)),
        "is_current": context.get("is_current"),
        "origin_verified": context.get("origin_verified"),
        "worktree_clean": context.get("worktree_clean"),
        "repository": context.get("repository"),
        "channel": context.get("channel", "stable"),
        "update_managed": context.get("kind") == "managed",
    }


def list_skills(context: dict) -> list[dict]:
    """仅解析清单明确注册的技能，拒绝路径穿越与指向项目外的符号链接。"""
    try:
        manifest = _manifest(context)
    except (OSError, ValueError, TypeError):
        return [{"name": None, "path": None, "status": "error", "message": "项目清单不可读取或格式无效"}]
    entries = manifest.get("skills")
    if not isinstance(entries, list) or not entries:
        return [{"name": None, "path": None, "status": "error", "message": "项目清单缺少非空 skills 注册表"}]
    root = _root(context)
    result = []
    names = set()
    for entry in entries:
        row = {"name": None, "path": None, "description": "", "status": "error", "message": "技能注册项无效"}
        if not isinstance(entry, dict):
            result.append(row)
            continue
        name, relative = entry.get("name"), entry.get("path")
        row["name"] = name if isinstance(name, str) else None
        row["description"] = entry.get("description") if isinstance(entry.get("description"), str) else ""
        if not isinstance(name, str) or not name.strip() or not isinstance(relative, str) or not relative.strip():
            result.append(row)
            continue
        if name in names:
            row["message"] = "技能名称重复"
            result.append(row)
            continue
        names.add(name)
        try:
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("技能路径必须为项目内的相对路径")
            resolved = (root / path).resolve()
            resolved.relative_to(root)
            instructions = (resolved / "SKILL.md").resolve()
            instructions.relative_to(root)
            row["path"] = str(resolved)
            if not resolved.is_dir() or not instructions.is_file():
                row["message"] = "技能目录或 SKILL.md 缺失"
            else:
                row.update(status="ok", message="技能已注册且入口存在", instructions=str(instructions))
        except (OSError, ValueError, RuntimeError):
            row["message"] = "技能路径越界或不可解析"
        result.append(row)
    return result


def _is_api_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname == "api.github.com"
                and parsed.port in (None, 443) and parsed.username is None and parsed.password is None)
    except ValueError:
        return False


class ReleaseRedirectHandler(HTTPRedirectHandler):
    """仅允许官方 API 同源 HTTPS 重定向。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _is_api_url(newurl):
            raise URLError("发布检查拒绝非同源 HTTPS 重定向")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _version(value: object) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or VERSION_PATTERN.fullmatch(value) is None:
        return None
    return tuple(int(part) for part in value.split("."))


def _release(payload: object) -> dict:
    if not isinstance(payload, dict) or payload.get("draft") is not False or payload.get("prerelease") is not False:
        raise ValueError("发布不是正式稳定版本")
    tag = payload.get("tag_name")
    if not isinstance(tag, str) or not tag.startswith("v") or _version(tag[1:]) is None:
        raise ValueError("发布标签不是 vX.Y.Z")
    url = f"{REPOSITORY_URL}/releases/tag/{tag}"
    if payload.get("html_url") != url:
        raise ValueError("发布链接与官方项目标签不一致")
    return {"tag_name": tag, "latest_version": tag[1:], "release_url": url}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            return None
        return (_now() - instant).total_seconds()
    except (ValueError, OverflowError):
        return None


def _cache_paths(context: dict) -> tuple[Path, Path, str]:
    identity = {
        "repository": context.get("repository"), "channel": context.get("channel", "stable"),
        "root": str(_root(context)), "current_commit": context.get("commit"),
        "current_version": context.get("version"),
    }
    key = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()
    home = Path(context.get("cache_home") or CACHE_HOME) / "update-checks"
    return home / f"{key}.json", home / f"{key}.error.json", key


def _read_cache(path: Path, key: str, ttl: int) -> dict | None:
    try:
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("cache_key") != key:
            return None
        age = _age(value.get("checked_at"))
        if age is None or age < -300 or age >= ttl:
            return None
        return value
    except (OSError, ValueError, TypeError):
        return None


def _unknown(context: dict, reason: str, *, checked_at: str | None = None, from_cache: bool = False) -> dict:
    return {
        "status": "unknown", "reason": reason, "current_version": context.get("version"),
        "latest_version": None, "tag_name": None, "release_url": None,
        "checked_at": checked_at, "from_cache": from_cache,
    }


def _comparison(context: dict, release: dict, checked_at: str, *, from_cache: bool) -> dict:
    current, latest = _version(context.get("version")), _version(release["latest_version"])
    if current is None:
        result = _unknown(context, "当前版本号无法比较", checked_at=checked_at, from_cache=from_cache)
        result.update(release)
        return result
    if current < latest:
        status, reason = "update_available", "发现新的稳定版本，可显式执行 aor update"
    elif current > latest:
        status, reason = "ahead", "当前版本高于最新稳定发布，不建议自动降级"
    else:
        status, reason = "up_to_date", "当前版本号与最新稳定发布一致"
    return {
        "status": status, "reason": reason, "current_version": context.get("version"),
        **release, "checked_at": checked_at, "from_cache": from_cache,
    }


def _store(path: Path, value: dict) -> None:
    try:
        atomic_json(path, value)
    except OSError:
        # 缓存不可写不影响本次研究任务或已经取得的检查结果。
        pass


def check_update(context: dict, refresh: bool = False, offline: bool = False) -> dict:
    """检查稳定发布；成功缓存一天，连接失败短暂缓存为未知，离线不读网络或写缓存。"""
    cache_path, error_path, key = _cache_paths(context)
    if offline or not refresh:
        cached = _read_cache(cache_path, key, CACHE_TTL_SECONDS)
        if cached is not None:
            try:
                release = _release(cached.get("release"))
                return _comparison(context, release, cached["checked_at"], from_cache=True)
            except (ValueError, TypeError):
                pass
        error = _read_cache(error_path, key, ERROR_CACHE_TTL_SECONDS)
        if error is not None and error.get("status") == "unknown":
            return _unknown(context, "最近检查因网络不可用而无法确认，稍后重试或使用 --refresh",
                            checked_at=error["checked_at"], from_cache=True)
    if offline:
        return _unknown(context, "离线模式且没有有效的发布检查缓存")
    checked_at = _now().isoformat()
    if context.get("channel", "stable") != "stable":
        return _unknown(context, "当前仅支持 stable 发布渠道")
    request = Request(LATEST_RELEASE_URL, headers={
        "Accept": "application/vnd.github+json", "User-Agent": "ai-opportunity-radar-release-check",
    }, method="GET")
    try:
        # 保留用户网络代理配置；不添加 GitHub 令牌、Cookie 或认证处理器。
        opener = build_opener(ProxyHandler(), ReleaseRedirectHandler())
        with opener.open(request, timeout=2.5) as response:
            if not _is_api_url(response.geturl()):
                raise ValueError("发布响应不在官方 HTTPS API")
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                raise ValueError("发布响应超过读取上限")
            payload = json.loads(body.decode("utf-8"))
        release = _release(payload)
    except HTTPError as error:
        reason = "尚无可用的稳定发布" if error.code == 404 else "发布服务暂不可用，无法确认最新版本"
        _store(error_path, {"cache_key": key, "checked_at": checked_at, "status": "unknown"})
        return _unknown(context, reason, checked_at=checked_at)
    except (URLError, OSError, TimeoutError):
        _store(error_path, {"cache_key": key, "checked_at": checked_at, "status": "unknown"})
        return _unknown(context, "网络不可用，无法确认最新版本", checked_at=checked_at)
    except (ValueError, TypeError, UnicodeError):
        return _unknown(context, "发布响应格式或来源校验失败，无法确认最新版本", checked_at=checked_at)
    _store(cache_path, {"cache_key": key, "checked_at": checked_at, "release": {
        "tag_name": release["tag_name"], "draft": False, "prerelease": False, "html_url": release["release_url"],
    }})
    return _comparison(context, release, checked_at, from_cache=False)


def doctor(context: dict, refresh: bool = False, offline: bool = False) -> dict:
    """汇总本地致命缺陷和可继续工作的提示，不自动修复或安装。"""
    root = _root(context)
    checks = []

    def add(name: str, status: str, message: str) -> None:
        checks.append({"name": name, "status": status, "message": message})

    add("project_root", "ok" if root.is_dir() else "error", "项目目录存在" if root.is_dir() else "项目目录缺失")
    try:
        manifest = _manifest(context)
        valid = _version(manifest.get("version")) is not None and manifest.get("instructions") == "SKILL.md"
        add("manifest", "ok" if valid else "error", "项目清单有效" if valid else "项目清单版本或指令入口无效")
    except (OSError, ValueError, TypeError):
        add("manifest", "error", "项目清单不可读取或格式无效")
    for name, relative in (("aor_entrypoint", "bin/aor"), ("radar_entrypoint", "scripts/radar.py"),
                           ("skill_instructions", "SKILL.md")):
        path = root / relative
        try:
            path.resolve().relative_to(root)
            valid = path.is_file()
        except (OSError, ValueError, RuntimeError):
            valid = False
        add(name, "ok" if valid else "error", f"{relative} 存在" if valid else f"{relative} 缺失或越界")
    python_ok = sys.version_info >= (3, 10)
    add("python", "ok" if python_ok else "error", f"Python {sys.version_info.major}.{sys.version_info.minor}；需要 3.10+")
    git = shutil.which("git")
    add("git", "ok" if git else "warning", "Git 可用" if git else "Git 不可用，更新需要先安装 Git")
    installation = get_status(context)
    add("installation", "ok" if installation["update_managed"] else "warning",
        "AOR 受管安装" if installation["update_managed"] else "当前安装可使用；自动替换仅支持 AOR 受管目录")
    if installation["update_managed"] and installation["origin_verified"] is False:
        add("origin", "error", "受管安装的来源未通过官方仓库验证，不能执行自动更新")
    if installation["kind"] in ("managed", "source") and installation["worktree_clean"] is False:
        add("worktree", "warning", "安装目录存在本地修改，aor update 不会覆盖这些修改")
    if installation["update_managed"] and installation["is_current"] is False:
        add("loaded_version", "warning", "当前进程仍加载旧版本目录；请使用 current 入口并重新读取技能")
    skills = list_skills(context)
    add("skills", "error" if any(row["status"] == "error" for row in skills) else "ok", "技能注册表已检查")
    updates = check_update(context, refresh=refresh, offline=offline)
    add("updates", "warning" if updates["status"] in ("unknown", "update_available") else "ok", updates["reason"])
    levels = {row["status"] for row in checks}
    health = "error" if "error" in levels else "warning" if "warning" in levels else "ok"
    return {"health": health, "installation": installation, "skills": skills, "updates": updates, "checks": checks}
