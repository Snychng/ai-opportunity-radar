#!/usr/bin/env python3
"""启动、恢复和查看文件化研究流程。"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import aor_bootstrap  # noqa: F401
from aor.workflow.research import (inspect_run, resume_research, reparse_run, run_paid_batch, run_discovery,
                                   run_comment_collection, start_research)
from build_query_plan import parse_date, read_scope, resolve_focus
from contracts import beijing_today
from manage_state import DEFAULT_HOME


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AOR 研究流程：程序执行确定性步骤，宿主 Agent 通过 JSON 文件补充判断")
    parser.add_argument("action", choices=("research", "resume", "inspect"))
    parser.add_argument("run_id", nargs="?")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--date", default=beijing_today().isoformat())
    parser.add_argument("--focus-file", type=Path)
    parser.add_argument("--scope-file", type=Path)
    parser.add_argument("--intent-plan-file", type=Path)
    parser.add_argument("--products-file", type=Path, help="产品、别名及任务，生成三平台体验/持续使用/切换搜索")
    parser.add_argument("--comments-file", type=Path, help="选择帖子及分页 policy，以明确预算采集评论和子回复")
    parser.add_argument("--parent-run-id")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--no-collect", action="store_true", help="本次恢复仅处理已有材料")
    parser.add_argument("--reparse", action="store_true", help="创建离线子运行，重解析已付费响应；不改原报告或再次请求")
    parser.add_argument("--include-comments", action=argparse.BooleanOptionalAction, default=True, help="默认采集免费社区评论；付费三平台用 --comments-file")
    parser.add_argument("--include-recent-activity", action="store_true", help="新研究额外检索旧 Issue 的近期活动")
    parser.add_argument("--concurrency", type=int, choices=range(1, 5), default=3, help="新研究免费检索并发数，1–4")
    parser.add_argument("--evidence", type=Path, action="append", default=[])
    parser.add_argument("--benchmarks", type=Path)
    parser.add_argument("--assessment", type=Path)
    parser.add_argument("--profile", type=Path)
    parser.add_argument("--paid-plan", type=Path, help="本轮明确缺口或评论补证计划")
    parser.add_argument("--discover", action="store_true", help="执行有明确预算的跨行业发现，跳过实时价格缺失来源")
    parser.add_argument("--max-discovery-requests", type=int, default=12, help="本次发现批次请求上限，默认 12")
    parser.add_argument("--max-cost-usd", type=float, help="本轮累计付费请求原价上限")
    parser.add_argument("--recurring-budget", action="store_true", help="使用已显式启用的共享持续预算；不会创建自动化")
    parser.add_argument("--batch-id", default="default")
    parser.add_argument("--resume-batch", action="store_true")
    parser.add_argument("--max-attempts", type=int, default=1)
    parser.add_argument("--resolve-unknown", action="append", default=[])
    parser.add_argument("--retry-failed", action="append", default=[])
    parser.add_argument("--json", action="store_true", help="兼容宿主约定；流程结果默认输出 JSON")
    args = parser.parse_args(argv)
    try:
        inputs = {"evidence_files": args.evidence, "benchmarks_file": args.benchmarks,
                  "assessment_file": args.assessment, "profile_file": args.profile}
        intent = json.loads(args.intent_plan_file.read_text(encoding="utf-8")) if args.intent_plan_file else None
        if args.products_file:
            if args.action != "research" or intent:
                raise ValueError("products-file 仅用于新研究，不能同时指定 intent-plan-file")
            from aor.sources.products import product_intents
            intent = product_intents(json.loads(args.products_file.read_text(encoding="utf-8")))
        if args.comments_file and (args.action != "resume" or not args.run_id or args.max_cost_usd is None
                                  or args.discover or args.paid_plan or args.reparse or args.offline or args.no_collect
                                  or args.recurring_budget or args.max_attempts != 1 or args.resolve_unknown or args.retry_failed
                                  or args.benchmarks or args.assessment or args.profile or args.evidence or intent):
            raise ValueError("评论分页使用 resume RUN_ID --comments-file FILE --max-cost-usd；失败/未知页面不自动重买")
        if args.recurring_budget and not (args.discover or args.paid_plan):
            raise ValueError("recurring-budget 仅适用于显式付费发现或补证")
        if args.reparse and (args.action != "resume" or not args.run_id or args.discover or args.paid_plan
                             or args.benchmarks or args.assessment or args.evidence or args.profile or intent):
            raise ValueError("离线重解析使用 resume RUN_ID --reparse；补充研究输入请在返回的新运行继续")
        if args.discover and (args.action != "resume" or not args.run_id or args.max_cost_usd is None or args.paid_plan):
            raise ValueError("付费发现需要 resume RUN_ID --discover --max-cost-usd，不能同时指定 paid-plan")
        if args.discover and (args.offline or args.no_collect):
            raise ValueError("离线或不采集模式不能执行付费发现")
        if args.paid_plan and (args.action != "resume" or not args.run_id or args.max_cost_usd is None):
            raise ValueError("付费补证需要 resume RUN_ID --paid-plan FILE --max-cost-usd 明确上限")
        if args.paid_plan and (args.offline or args.no_collect):
            raise ValueError("本次指定离线或不采集，不能执行付费请求")
        if args.action == "research":
            if args.run_id:
                raise ValueError("新研究自动分配运行 ID；恢复请使用 resume RUN_ID")
            result = start_research(args.home, as_of=parse_date(args.date),
                                    focus=resolve_focus(None, args.focus_file), scope=read_scope(args.scope_file),
                                    intent_plan=intent, offline=args.offline or args.no_collect,
                                    include_comments=args.include_comments, include_recent_activity=args.include_recent_activity,
                                    concurrency=args.concurrency,
                                    parent_run_id=args.parent_run_id, **inputs)
        elif not args.run_id:
            raise ValueError(f"{args.action} 必须指定 RUN_ID")
        elif args.action == "resume":
            if args.comments_file:
                result = run_comment_collection(args.home, args.run_id, args.comments_file,
                            max_cost_usd=args.max_cost_usd, batch_id=args.batch_id, resume=args.resume_batch)
            elif args.reparse:
                result = reparse_run(args.home, args.run_id)
            elif args.discover:
                result = run_discovery(args.home, args.run_id, max_cost_usd=args.max_cost_usd,
                                       batch_id=args.batch_id, max_requests=args.max_discovery_requests,
                                       resume=args.resume_batch, max_attempts=args.max_attempts,
                                       resolve_unknown=tuple(args.resolve_unknown), retry_failed=tuple(args.retry_failed),
                                       recurring=args.recurring_budget)
            elif args.paid_plan:
                result = run_paid_batch(args.home, args.run_id, args.paid_plan, max_cost_usd=args.max_cost_usd,
                                        batch_id=args.batch_id, resume=args.resume_batch, max_attempts=args.max_attempts,
                                        resolve_unknown=tuple(args.resolve_unknown), retry_failed=tuple(args.retry_failed),
                                        recurring=args.recurring_budget)
            else:
                result = resume_research(args.home, args.run_id, collect=not (args.offline or args.no_collect), intent_plan=intent, **inputs)
        else:
            result = inspect_run(args.home, args.run_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from aor_runtime import run_legacy

    raise SystemExit(run_legacy(main, __file__))
