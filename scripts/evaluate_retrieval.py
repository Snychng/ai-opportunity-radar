#!/usr/bin/env python3
"""对真实缓存材料做检索质量统计，不发起请求。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import aor_bootstrap  # noqa: F401
from aor.evidence.benchmark import evaluate_retrieval
from evidence_library import read_records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--labels", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--estimated-cost-usd", type=float)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        records, _ = read_records(args.evidence)
        result = evaluate_retrieval(records, json.loads(args.labels.read_text()), as_of=args.as_of,
                                    estimated_cost_usd=args.estimated_cost_usd)
        text = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
        else:
            print(text, end="")
        return 0
    except (ValueError, OSError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    raise SystemExit(main())
