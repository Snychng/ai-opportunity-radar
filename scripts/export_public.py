#!/usr/bin/env python3
"""校验并导出网站契约，或从统一 Schema 生成前端类型。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import aor_bootstrap  # noqa: F401
from aor.reporting.public import write_public_bundle
from aor.reporting.public_contract import typescript, validate_public_dataset


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="导出网站公开数据，保留内部研究档案")
    parser.add_argument("report", type=Path, nargs="?")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--validate", type=Path, help="校验已导出的 public.v1.json")
    parser.add_argument("--consumer", action="store_true", help="消费端兼容同主版本的新增字段")
    parser.add_argument("--types", type=Path, help="从 Schema 生成 TypeScript")
    parser.add_argument("--site-report", type=Path, action="append", default=[], help="累积全站目录，重复传入已修订研究报告")
    parser.add_argument("--site-id", default="SITE-DEFAULT", help="全站目录的稳定身份")
    parser.add_argument("--as-of", help="复核截止日 YYYY-MM-DD，默认使用最新报告日期")
    parser.add_argument("--review-period-days", type=int, default=30, help="来源复核周期")
    parser.add_argument("--due-soon-days", type=int, default=7, help="到期前提醒天数")
    parser.add_argument("--json", action="store_true", help="兼容入口；默认已输出 JSON")
    args = parser.parse_args(argv)
    freshness = dict(as_of=args.as_of, review_period_days=args.review_period_days,
                     due_soon_days=args.due_soon_days)
    try:
        if args.types:
            args.types.parent.mkdir(parents=True, exist_ok=True)
            args.types.write_text(typescript(), encoding="utf-8")
            result = {"types": str(args.types)}
        elif args.validate:
            result = validate_public_dataset(json.loads(args.validate.read_text()), consumer=args.consumer)
            if not result["valid"]:
                print(json.dumps(result, ensure_ascii=False, indent=2))
                return 1
        elif args.site_report and args.output:
            if args.report:
                raise ValueError("单次报告与全站报告列表不能混用")
            from aor.reporting.site import write_site_bundle
            result = write_site_bundle([json.loads(p.read_text()) for p in args.site_report], args.output,
                                       site_id=args.site_id, **freshness)
        elif args.report and args.output:
            result = write_public_bundle(json.loads(args.report.read_text()), args.output, **freshness)
        else:
            parser.error("需要 REPORT --output DIR、--site-report FILE --output DIR、--validate FILE 或 --types FILE")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from aor_runtime import run_legacy
    raise SystemExit(run_legacy(main, __file__))
