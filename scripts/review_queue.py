#!/usr/bin/env python3
"""批量语义审阅任务的本地 CLI；不访问网络和付费接口。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import aor_bootstrap  # noqa: F401
from aor.workflow.review_queue import ReviewQueue
from manage_state import DEFAULT_HOME


def main(argv=None):
    parser = argparse.ArgumentParser(description="创建、领取和提交精确修订的语义审阅任务")
    parser.add_argument("action", choices=("plan", "claim", "submit", "release", "status"))
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--library-root", type=Path)
    parser.add_argument("--input", type=Path, help="plan 的引用索引或 submit 的结果 JSON")
    parser.add_argument("--max-items", type=int, default=16)
    parser.add_argument("--max-chars", type=int, default=18000)
    parser.add_argument("--worker")
    parser.add_argument("--lease-seconds", type=int, default=1800)
    parser.add_argument("--lease-id")
    parser.add_argument("--batch-id")
    parser.add_argument("--industries", nargs="+", help="只领取属于所列行业的未审任务")
    parser.add_argument("--retry-deferred", action="store_true", help="允许重新领取 unknown/needs_fulltext；它们不算已审")
    parser.add_argument("--reason")
    parser.add_argument("--json", action="store_true", help="兼容入口，默认即 JSON")
    args = parser.parse_args(argv)
    try:
        queue = ReviewQueue(args.home, args.run_id, library_root=args.library_root)
        if args.action == "plan":
            result = queue.plan(input_path=args.input, max_items=args.max_items, max_chars=args.max_chars)
        elif args.action == "claim":
            result = queue.claim(args.worker, lease_seconds=args.lease_seconds, batch_id=args.batch_id,
                                 industries=args.industries, retry_deferred=args.retry_deferred)
        elif args.action == "submit":
            if args.input is None:
                raise ValueError("submit 需要 --input JSON")
            payload = json.loads(args.input.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("提交输入必须是 JSON 对象")
            result = queue.submit(args.lease_id or payload.get("lease_id"), args.worker or payload.get("worker"),
                                  payload.get("submission_id"), payload.get("results"))
        elif args.action == "release":
            result = queue.release(args.lease_id, args.worker, reason=args.reason)
        else:
            result = queue.status()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
