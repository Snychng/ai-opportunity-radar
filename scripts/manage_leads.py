#!/usr/bin/env python3
"""记录探索线索的修订状态；正式升级关联已经入库的机会。"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
import aor_bootstrap  # noqa: F401
from aor.opportunity.exploration import LEAD_STATES, current_lead_evidence, lead_history, transition_lead
from contracts import beijing_today
from manage_state import DEFAULT_HOME


def main(argv=None):
    parser = argparse.ArgumentParser(description="查看或修订探索线索状态")
    parser.add_argument("action", choices=("list", "transition"))
    parser.add_argument("lead_id", nargs="?")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--date", default=beijing_today().isoformat())
    parser.add_argument("--status", choices=sorted(LEAD_STATES))
    parser.add_argument("--reason")
    parser.add_argument("--run-id")
    parser.add_argument("--promoted-to")
    parser.add_argument("--json", action="store_true", help="兼容入口；默认已输出 JSON")
    args = parser.parse_args(argv)
    try:
        if args.action == "list":
            result = {"as_of": args.date, "leads": lead_history(args.home, as_of=args.date,
                       evidence=current_lead_evidence(args.home, as_of=args.date))}
        else:
            if not args.lead_id or not args.run_id or not args.status or not args.reason:
                raise ValueError("状态变更需要 lead_id、run-id、status 和 reason")
            record = None
            if args.promoted_to:
                for name in ("opportunities", "signals"):
                    path = args.home / "state" / f"{name}.jsonl"
                    for line in path.read_text().splitlines() if path.exists() else []:
                        row = json.loads(line)
                        if row.get("id") == args.promoted_to:
                            record = row
                if record is None:
                    raise ValueError("关联的正式记录未入库")
            result = transition_lead(args.home, args.lead_id, status=args.status, reason=args.reason,
                                     run_id=args.run_id, as_of=args.date, promoted_record=record)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    from aor_runtime import run_legacy
    raise SystemExit(run_legacy(main, __file__))
