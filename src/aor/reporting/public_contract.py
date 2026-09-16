"""网站公开契约：从同一 JSON Schema 校验数据并生成 TypeScript。运行时无第三方依赖。"""

from __future__ import annotations

from datetime import date, datetime, timedelta
import json
import math
from pathlib import Path
import re
from urllib.parse import urlparse

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schemas/public-v1.schema.json"


def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def validate_public_dataset(value: dict, *, consumer: bool = False) -> dict:
    """严格生产端；消费端只容忍同主版本的未知字段，仍校验已知字段与引用。"""
    root = schema()
    errors = []

    def check(node, spec, path):
        if "$ref" in spec:
            spec = root["$defs"][spec["$ref"].rsplit("/", 1)[-1]]
        for operator in ("oneOf", "anyOf"):
            if operator in spec:
                matches = 0
                details = []
                for alternative in spec[operator]:
                    start = len(errors)
                    check(node, alternative, path)
                    if len(errors) == start:
                        matches += 1
                    else:
                        details.extend(errors[start:])
                    del errors[start:]
                if not matches or (operator == "oneOf" and matches != 1):
                    errors.append(f"{path}: 视图或联合类型不符")
                    errors.extend(details[:12])
                return
        types = spec.get("type")
        if isinstance(types, str):
            types = [types]
        actual = ("null" if node is None else "boolean" if isinstance(node, bool) else "integer" if isinstance(node, int)
                  else "number" if isinstance(node, float) else "string" if isinstance(node, str)
                  else "array" if isinstance(node, list) else "object" if isinstance(node, dict) else "invalid")
        if types and actual not in types and not (actual == "integer" and "number" in types):
            errors.append(f"{path}: 类型不符")
            return
        if "enum" in spec and node not in spec["enum"]:
            errors.append(f"{path}: 非法枚举值")
        if "const" in spec and (type(node) is not type(spec["const"]) or node != spec["const"]):
            errors.append(f"{path}: 契约版本或固定值不符")
        if isinstance(node, dict):
            for key in spec.get("required", []):
                if key not in node:
                    errors.append(f"{path}.{key}: 必填")
            properties = spec.get("properties", {})
            for key, child in node.items():
                if key in properties:
                    check(child, properties[key], f"{path}.{key}")
                elif spec.get("additionalProperties") is False and not consumer:
                    errors.append(f"{path}.{key}: 不允许公开此字段")
        elif isinstance(node, list):
            if len(node) < spec.get("minItems", 0) or len(node) > spec.get("maxItems", math.inf):
                errors.append(f"{path}: 项目数量不符")
            if spec.get("uniqueItems") and len({json.dumps(v, sort_keys=True) for v in node}) != len(node):
                errors.append(f"{path}: 重复项目")
            for index, child in enumerate(node):
                check(child, spec.get("items", {}), f"{path}[{index}]")
        elif isinstance(node, str):
            if len(node.strip()) < spec.get("minLength", 0) or len(node) > spec.get("maxLength", math.inf):
                errors.append(f"{path}: 文本长度不符")
            if spec.get("pattern") and not re.search(spec["pattern"], node):
                errors.append(f"{path}: 格式不符")
            try:
                if spec.get("format") == "date":
                    date.fromisoformat(node)
                elif spec.get("format") == "date-time":
                    if datetime.fromisoformat(node.replace("Z", "+00:00")).tzinfo is None:
                        raise ValueError("缺少时区")
                elif spec.get("format") == "uri":
                    parsed = urlparse(node)
                    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                        raise ValueError("非公开地址")
            except ValueError:
                errors.append(f"{path}: 日期或链接格式不符")
        elif actual in {"integer", "number"}:
            if not math.isfinite(node) or node < spec.get("minimum", -math.inf) or node > spec.get("maximum", math.inf):
                errors.append(f"{path}: 数值不符")

    check(value, root, "$")
    if not errors:
        source_runs = {row["run_id"]: row for row in value["source_runs"]}
        if len(source_runs) != len(value["source_runs"]):
            errors.append("来源运行 ID 重复")
        review = value["quality"].get("review_summary")
        if review:
            if review["source_run_id"] not in source_runs:
                errors.append("审阅汇总引用未知运行")
            if review["reviewed_count"] + review["unreviewed_count"] != review["evidence_count"]:
                errors.append("审阅汇总数量不一致")
        if any(row["as_of"] > value["as_of"] for row in source_runs.values()):
            errors.append("来源运行晚于目录截止日")
        if value["snapshot_scope"] == "research_run":
            if value["site_manifest"] is not None or len(source_runs) != 1 or value["dataset_id"] not in source_runs:
                errors.append("单次研究快照的来源或站点清单不一致")
        else:
            manifest = value["site_manifest"]
            if not re.fullmatch(r"SITE-[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value["dataset_id"]) or manifest is None:
                errors.append("全站目录缺少稳定 SITE 标识或清单")
            elif manifest["source_run_count"] != len(source_runs) or manifest["latest_run_id"] not in source_runs:
                errors.append("全站清单来源运行不一致")
            else:
                tombstone_ids = [row["id"] for row in manifest["tombstones"]]
                if len(set(tombstone_ids)) != len(tombstone_ids):
                    errors.append("全站撤回记录 ID 重复")
                removed = {row["id"] for row in manifest["tombstones"] if row["reason"] == "scope_excluded"}
                if removed != set(manifest["scope_removed_ids"]):
                    errors.append("范围移除清单与撤回记录不一致")
                for tombstone in manifest["tombstones"]:
                    if tombstone["run_id"] not in source_runs or tombstone["as_of"] > value["as_of"]:
                        errors.append("撤回记录引用未知运行或未来日期")
                if value["view"] != "detail":
                    by_id = {row["id"]: row for row in value["items"]}
                    for tombstone in manifest["tombstones"]:
                        if tombstone["reason"] == "scope_excluded":
                            if tombstone["id"] in by_id:
                                errors.append("范围排除内容仍出现在全站目录")
                        elif by_id.get(tombstone["id"], {}).get("publication_status") != "withdrawn":
                            errors.append("撤回记录没有对应的撤回内容")
                    if manifest["published_count"] != sum(row["publication_status"] == "published" for row in value["items"]):
                        errors.append("全站已发布数量与内容不一致")
                    if manifest["withdrawn_count"] != sum(row["publication_status"] == "withdrawn" for row in value["items"]):
                        errors.append("全站撤回数量与内容不一致")
                    retained = sum(row["publication_status"] == "published" and row["last_source_run_id"] != manifest["latest_run_id"]
                                   for row in value["items"])
                    if manifest["retained_from_history_count"] != retained:
                        errors.append("全站保留历史内容数量不一致")
        industry_ids = {r["id"] for r in value["industries"]}
        if len(industry_ids) != len(value["industries"]):
            errors.append("行业目录 ID 重复")
        subtracks = {r["id"]: {s["id"] for s in r["subtracks"]} for r in value["industries"]}
        if any(len(subtracks[r["id"]]) != len(r["subtracks"]) for r in value["industries"]):
            errors.append("行业内子赛道 ID 重复")
        coverage_ids = [row["industry_id"] for row in value["coverage"]]
        if len(set(coverage_ids)) != len(coverage_ids) or not set(coverage_ids) <= industry_ids:
            errors.append("覆盖引用未知或重复行业")
        for row in value["coverage"]:
            if row["reviewed_count"] + row["unreviewed_count"] != row["material_count"]:
                errors.append("已审与未审数量必须等于材料数量")
            if any(row[key] > row["reviewed_count"] for key in
                   ("recent_demand_count", "commercial_evidence_count", "counterevidence_count")):
                errors.append("证据维度数量不能超过已审材料数量")
        records = value.get("evidence") or []
        policy = value.get("freshness_policy")
        if policy and policy["due_soon_days"] >= policy["review_period_days"]:
            errors.append("到期提醒天数必须小于复核周期")
        evidence_by_id = {(r["id"], r["revision_id"]): r for r in records}
        for record in records:
            if any(record.get(k) and record[k] > value["as_of"] for k in ("source_observed_at", "semantic_reviewed_at")):
                errors.append("证据复核或来源观察日期晚于截止日")
        for item in value["items"]:
            freshness = item.get("freshness")
            if not freshness:
                continue  # 旧版 public 1.0 没有复核期限字段，保持可读。
            if not policy:
                errors.append("复核期限缺少计算周期")
                continue
            if freshness["as_of"] != value["as_of"]:
                errors.append("条目复核截止日与快照不一致")
            if any(freshness[k] and freshness[k] > value["as_of"] for k in
                   ("last_verified_at", "source_observed_at", "semantic_reviewed_at", "publication_checked_at")):
                errors.append("条目核验日期晚于快照截止日")
            source, semantic = freshness["source_observed_at"], freshness["semantic_reviewed_at"]
            verified = min(source, semantic) if source and semantic else None
            due = (date.fromisoformat(verified) + timedelta(days=policy["review_period_days"])).isoformat() if verified else None
            status = ("unknown" if due is None else "overdue" if value["as_of"] > due else
                      "due" if value["as_of"] >= (date.fromisoformat(due) - timedelta(days=policy["due_soon_days"])).isoformat() else "fresh")
            if (freshness["last_verified_at"], freshness["review_due_at"], freshness["status"]) != (verified, due, status):
                errors.append("复核期限或状态与来源观察和语义复核日期不一致")
            if value["view"] != "index":
                needed = [evidence_by_id.get((ref["id"], ref["revision_id"]), {})
                          for ref in [*item["evidence_refs"], *item["ai_value"]["evidence_refs"]]]
                for field in ("source_observed_at", "semantic_reviewed_at"):
                    dates = [record.get(field) for record in needed]
                    oldest = min(dates) if dates and all(dates) else None
                    if freshness[field] != oldest:
                        errors.append("条目复核日期必须覆盖全部必要证据修订")
        refs = {(r["id"], r["revision_id"]) for r in records}
        if len(refs) != len(records):
            errors.append("证据修订重复")
        ids = set()
        for item in value["items"]:
            if item["id"] in ids:
                errors.append("内容 ID 重复")
            ids.add(item["id"])
            if not set(item["source_run_ids"]) <= source_runs.keys() or item["last_source_run_id"] not in item["source_run_ids"]:
                errors.append("内容缺少合法的来源运行关联")
            if not set(item["industry_ids"]) <= industry_ids:
                errors.append("内容引用未知行业")
            expected = "regional_hypothesis" if item["evidence_tier"] == "R" else "candidate" if item["evidence_tier"] else None
            if (expected and item["stage"] != expected) or (not expected and item["stage"] in {"candidate", "regional_hypothesis"}):
                errors.append("内容阶段与证据等级不一致")
            if value["view"] == "index":
                continue
            known_subtracks = set().union(*(subtracks.get(key, set()) for key in item["industry_ids"]))
            if not set(item["subtrack_ids"]) <= known_subtracks:
                errors.append("内容子赛道不属于所选行业")
            for ref in [*item["evidence_refs"], *item["ai_value"]["evidence_refs"]]:
                if (ref["id"], ref["revision_id"]) not in refs:
                    errors.append("内容引用未知证据修订")
            if item["publication_status"] == "published" and not item["evidence_refs"]:
                errors.append("已发布内容必须有可追溯证据")
            if item["publication_status"] == "published" and item["ai_value"]["status"] == "supported" and not item["ai_value"]["evidence_refs"]:
                errors.append("AI 已支持结论必须引用证据")
            if item["updated_at"] > value["as_of"]:
                errors.append("内容更新时间晚于研究截止日")
    return {"valid": not errors, "errors": errors}


def typescript() -> str:
    root = schema()

    def ts(spec):
        if "$ref" in spec:
            return spec["$ref"].rsplit("/", 1)[-1]
        if "const" in spec:
            return json.dumps(spec["const"])
        if "oneOf" in spec or "anyOf" in spec:
            return " | ".join(ts(value) for value in spec.get("oneOf", spec.get("anyOf")))
        if "enum" in spec:
            return " | ".join(json.dumps(v) for v in spec["enum"])
        kind = spec.get("type")
        if isinstance(kind, list):
            return " | ".join(ts({**spec, "type": v}) for v in kind)
        if kind == "array":
            return f"Array<{ts(spec.get('items', {}))}>"
        if kind == "object":
            fields = [f"{json.dumps(k)}{'' if k in spec.get('required', []) else '?'}: {ts(v)};"
                      for k, v in spec.get("properties", {}).items()]
            if spec.get("additionalProperties") is not False:
                fields.append("[key: string]: unknown;")
            return "{ " + " ".join(fields) + " }"
        return {"string": "string", "integer": "number", "number": "number", "null": "null", "boolean": "boolean"}.get(kind, "unknown")

    return ("// 由 schemas/public-v1.schema.json 生成；请勿手改。\n" +
            "\n".join(f"export type {name} = {ts(spec)};" for name, spec in root["$defs"].items()) +
            f"\nexport type PublicDocument = {ts(root)};\n")
