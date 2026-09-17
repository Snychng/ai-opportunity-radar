#!/usr/bin/env python3
"""多渠道检索计划、隔离收集与协调者回执导入。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import aor_bootstrap  # noqa: F401
from aor.workflow.search import collect_search, import_search, plan_search, read_config, search_status
from manage_state import DEFAULT_HOME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "collect", "import", "status"))
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--task-id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--input", type=Path, action="append", default=[])
    parser.add_argument("--max-cost-usd", type=float)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--offline", action="store_true", help="本次命令不执行真实请求")
    args = parser.parse_args(argv)
    try:
        if args.action != "collect" and (args.task_id or args.output or args.max_cost_usd is not None):
            raise ValueError("task-id、output、max-cost-usd 仅用于 collect")
        if args.action != "import" and args.input:
            raise ValueError("input 仅用于 import")
        if args.action == "plan":
            result = plan_search(args.home, args.run_id, config=read_config(args.config, home=args.home))
        elif args.action == "collect":
            if not args.task_id:
                raise ValueError("collect 需要 --task-id")
            result = collect_search(args.home, args.run_id, args.task_id, config=read_config(args.config, home=args.home), output=args.output,
                                    max_cost_usd=args.max_cost_usd, offline=args.offline)
        elif args.action == "import":
            result = import_search(args.home, args.run_id, args.input)
        else:
            result = search_status(args.home, args.run_id)
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
