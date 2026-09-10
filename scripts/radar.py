#!/usr/bin/env python3
"""面向任何具备命令执行能力的 AI Agent 的统一入口。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from aor_runtime import AorError, enabled, installation_context, lock, preflight, process_environment, update_notice

COMMANDS = {
    "plan": "build_query_plan.py",
    "community": "community_query.py",
    "paid": "tikhub_query.py",
    "normalize": "normalize_tikhub_results.py",
    "expand": "expand_ideas.py",
    "filter": "filter_ideas.py",
    "score": "score_candidates.py",
    "state": "manage_state.py",
    "digest": "build_result_digest.py",
    "report": "validate_report.py",
    "validation": "manage_validation.py",
}


def _json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _management(command: str, arguments: list[str], context: dict) -> int:
    parser = argparse.ArgumentParser(prog=f"aor {command}")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    if command == "doctor":
        output.add_argument("--quiet", action="store_true", help="仅提示新版本或本地致命错误，适合调用技能时检查")
        options = parser.add_mutually_exclusive_group()
        options.add_argument("--refresh", action="store_true", help="跳过缓存并检查稳定 Release")
        options.add_argument("--offline", action="store_true", help="仅检查本地与已有缓存")
    if command == "install":
        parser.add_argument("--source", type=Path, help="显式从干净 Git 源码离线安装，保留来源信息")
        parser.add_argument("--home", type=Path, help="受管安装目录，独立于研究数据")
        parser.add_argument("--bin-dir", type=Path, help="aor 可执行文件安装目录")
    args = parser.parse_args(arguments)
    if command == "skills":
        from aor_status import list_skills

        result = list_skills(context)
        if args.json:
            _json(result)
        else:
            from aor_display import print_skills

            print_skills(context, result)
        return 0
    if command == "doctor":
        from aor_status import doctor

        result = doctor(context, refresh=args.refresh, offline=args.offline or enabled("AOR_OFFLINE"))
        if args.quiet:
            notice = update_notice(result.get("updates", {}))
            if notice:
                print(notice, file=sys.stderr)
            if result["health"] == "error":
                errors = [check for check in result.get("checks", []) if check["status"] == "error"]
                for check in errors:
                    print(f"AOR 检查失败 [{check['name']}]：{check['message']}", file=sys.stderr)
                if not errors:
                    print("AOR 本地检查失败；运行 aor doctor 查看详情。", file=sys.stderr)
        elif args.json:
            _json(result)
        else:
            print(f"AOR 诊断：{result['health']}")
            for check in result.get("checks", []):
                print(f"[{check['status']}] {check['name']}：{check['message']}")
            print(f"版本：{context.get('version')}；路径：{context['root']}")
            updates = result.get("updates", {})
            print(f"稳定更新：{updates.get('status')}；最新版本：{updates.get('latest_version') or '未知'}")
            notice = update_notice(updates)
            if notice:
                print(notice)
        return 1 if result["health"] == "error" else 0
    from aor_install import install, install_local, update

    if command == "install":
        action = install_local if args.source else install
        result = action(args.source or context["root"], home=args.home, bin_dir=args.bin_dir)
    else:
        result = update(context)
    if args.json:
        _json(result)
    else:
        print(f"AOR {command}：{result.get('status')}；版本：{result.get('version') or context.get('version')}")
        if result.get("bin_path"):
            print(f"命令：{result['bin_path']}")
        if result.get("current_path"):
            print(f"Skill 入口：{result['current_path']}/SKILL.md")
        if result.get("reload_required"):
            print("请让 Agent 重新读取当前 SKILL.md 与本次使用的参考文件。")
    return 0


def main(argv: list[str] | None = None) -> int:
    context = installation_context()
    parser = argparse.ArgumentParser(description="AI Opportunity Radar：通用 Agent 研究与验证工具。使用 COMMAND --help 查看参数。")
    parser.add_argument("--version", action="version", version=context.get("version") or "unknown")
    parser.add_argument("--json", action="store_true", help="以 JSON 显示当前状态")
    parser.add_argument("command", nargs="?", choices=sorted({*COMMANDS, "skills", "doctor", "update", "install"}))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        if args.command is None:
            from aor_status import check_update, get_status, list_skills

            result = get_status(context)
            result["skills"] = list_skills(context)
            result["updates"] = check_update(context, offline=True)
            if args.json:
                _json(result)
            else:
                from aor_display import print_overview

                print_overview(context, result["skills"])
                sys.stdout.flush()
                notice = update_notice(result["updates"])
                if notice:
                    print(notice, file=sys.stderr)
            return 0
        if args.command not in COMMANDS:
            arguments = ["--json", *args.arguments] if args.json else args.arguments
            return _management(args.command, arguments, context)
        if context["kind"] == "managed":
            with lock(context["home"]):
                return _research(args, context)
        return _research(args, context)
    except (AorError, ValueError, OSError) as exc:
        if args.json or "--json" in args.arguments:
            _json({"status": "error", "message": str(exc)})
        else:
            print(f"AOR：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


def _research(args: argparse.Namespace, context: dict) -> int:
    preflight(context)
    script = Path(__file__).resolve().parent / COMMANDS[args.command]
    environment = process_environment(PYTHONDONTWRITEBYTECODE="1", AOR_NO_UPDATE_CHECK="1")
    return subprocess.run([sys.executable, str(script), *args.arguments], env=environment, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
