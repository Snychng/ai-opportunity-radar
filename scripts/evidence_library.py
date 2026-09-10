#!/usr/bin/env python3
"""本地证据库兼容入口；所有命令仅操作离线文件。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import aor_bootstrap  # noqa: F401
from aor.evidence.claims import build_evidence_packet, validate_claims  # noqa: F401
from aor.evidence.retrieval import reciprocal_rank_fusion  # noqa: F401
from aor.storage.evidence_library import EvidenceLibrary, EvidenceLibraryError


def read_records(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """读取 JSON/JSONL 或带 evidence 数组的阶段产物。"""
    text = path.read_text(encoding="utf-8")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in text.splitlines() if line.strip()]
    metadata = payload if isinstance(payload, dict) else {}
    if isinstance(payload, dict) and isinstance(payload.get("evidence"), list):
        records = payload["evidence"]
    elif isinstance(payload, dict):
        records = [payload]
    else:
        records = payload
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise EvidenceLibraryError("输入须为证据对象、数组、JSONL 或带 evidence 的阶段对象")
    return records, metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="可重建的本地证据索引与研究上下文")
    parser.add_argument("--root", type=Path, required=True, help="专用证据库目录")
    parser.add_argument("--output", type=Path, help="输出 JSON 文件")
    commands = parser.add_subparsers(dest="command", required=True)
    ingest = commands.add_parser("ingest", help="导入离线 JSON/JSONL")
    ingest.add_argument("--input", type=Path, required=True)
    ingest.add_argument("--as-of", required=True)
    ingest.add_argument("--run-id")
    for command in ("search", "context"):
        sub = commands.add_parser(command)
        sub.add_argument("--query", action="append", default=[], help="可重复，多个查询以 RRF 融合")
        sub.add_argument("--as-of", required=True)
        sub.add_argument("--run-id")
        sub.add_argument("--limit", type=int, default=20)
        if command == "context":
            sub.add_argument("--claims", type=Path)
            sub.add_argument("--experiments", type=Path)
            sub.add_argument("--max-chars", type=int, default=12000)
    delta = commands.add_parser("delta")
    delta.add_argument("--since", required=True)
    delta.add_argument("--as-of", required=True)
    delta.add_argument("--run-id")
    commands.add_parser("rebuild", help="完全从 JSONL 重建 SQLite")
    args = parser.parse_args(argv)
    library = EvidenceLibrary(args.root)
    try:
        if args.command == "ingest":
            records, metadata = read_records(args.input)
            if metadata.get("as_of") and metadata["as_of"] != args.as_of:
                raise EvidenceLibraryError("输入 as_of 与命令不一致；复用历史请使用 search/context")
            if args.run_id and metadata.get("run_id") and args.run_id != metadata["run_id"]:
                raise EvidenceLibraryError("输入 run_id 与命令不一致")
            result = library.ingest(records, as_of=args.as_of, run_id=args.run_id or metadata.get("run_id"),
                                    raw_ref=str(args.input.resolve()))
        elif args.command == "search":
            result = {"schema_version": "3.0", "as_of": args.as_of, "run_id": args.run_id,
                      "evidence": library.search(args.query or "", as_of=args.as_of, run_id=args.run_id, limit=args.limit)}
        elif args.command == "context":
            claims = json.loads(args.claims.read_text(encoding="utf-8")) if args.claims else None
            experiments = json.loads(args.experiments.read_text(encoding="utf-8")) if args.experiments else None
            result = library.context(args.query or "", as_of=args.as_of, run_id=args.run_id, claims=claims,
                                     experiments=experiments, max_items=args.limit, max_chars=args.max_chars)
        elif args.command == "delta":
            result = library.delta(since=args.since, as_of=args.as_of, run_id=args.run_id)
        else:
            result = library.rebuild()
        serialized = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
