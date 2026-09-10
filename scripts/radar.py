#!/usr/bin/env python3
"""面向任何具备命令执行能力的 AI Agent 的统一入口。"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

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


def main() -> int:
    parser = argparse.ArgumentParser(description="AI Opportunity Radar：通用 Agent 研究与验证工具。使用 COMMAND --help 查看参数。")
    parser.add_argument("command", choices=sorted(COMMANDS))
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    script = Path(__file__).resolve().parent / COMMANDS[args.command]
    try:
        return subprocess.run([sys.executable, str(script), *args.arguments], check=False).returncode
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
