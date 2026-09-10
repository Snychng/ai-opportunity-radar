#!/usr/bin/env python3
"""来源能力目录、无网络配置诊断和宿主网页证据导入。"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import aor_bootstrap  # noqa: F401
from aor.sources.importing import import_web_evidence, read_json_file
from aor.sources.registry import diagnose_sources, source_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("catalog", "diagnose", "import"):
        child = subparsers.add_parser(command)
        child.add_argument("--output", type=Path)
        child.add_argument("--json", action="store_true", help="兼容统一 CLI；默认即为 JSON")
        if command == "import":
            child.add_argument("--input", type=Path, required=True)
            child.add_argument("--run-id", required=True)
            child.add_argument("--as-of", required=True)
    args = parser.parse_args()
    try:
        if args.command == "catalog":
            result = {"sources": source_catalog(), "live_health": "not_checked"}
        elif args.command == "diagnose":
            result = diagnose_sources({"TIKHUB_API_KEY": bool(os.environ.get("TIKHUB_API_KEY")),
                                       "GITHUB_TOKEN": bool(os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN"))})
        else:
            result = import_web_evidence(read_json_file(args.input), run_id=args.run_id, as_of=args.as_of)
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    # 此入口保证无网络，不运行可能访问更新服务器的 run_legacy 预检。
    raise SystemExit(main())
