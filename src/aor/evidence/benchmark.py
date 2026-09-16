"""真实检索材料的独立标注统计；不把词面命中或供应商宣传算作用户需求。"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timezone
import math
from typing import Any

from aor.evidence.quality import research_window, window_status

LABELS = {
    "query_match": {"direct", "adjacent", "unrelated", "unknown"},
    "actor": {"user", "supplier", "editorial", "unknown"},
    "signal": {"task", "payment", "alternative", "promotion", "discussion", "unknown"},
}


def _key(row: dict) -> tuple[str, str]:
    if not isinstance(row, dict):
        raise ValueError("材料与标注必须为对象")
    key = row.get("evidence_id"), row.get("revision_id")
    if not all(isinstance(value, str) and value.strip() for value in key):
        raise ValueError("每条材料与标注都需要 evidence_id / revision_id")
    return key


def _day(value: Any) -> date:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return (parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed).date()


def _body(row: dict) -> list[str]:
    # 评论必须绑定自己的对象身份；不能借父帖的日期和查询重复计算。
    return [row[k] for k in ("original_text", "text") if isinstance(row.get(k), str)]


def _counts(rows: list[dict], labels: dict, as_of: str) -> dict:
    reviewed = [labels[_key(row)] for row in rows if _key(row) in labels]
    direct = sum(label["actor"] == "user" and label["query_match"] == "direct"
                 and label["signal"] in {"task", "payment", "alternative"} for label in reviewed)
    direct_dates = Counter()
    for row in rows:
        label = labels.get(_key(row), {})
        if label.get("actor") == "user" and label.get("query_match") == "direct" and label.get("signal") in {"task", "payment", "alternative"}:
            direct_dates[window_status(row.get("published_at"), as_of=as_of,
                                      interval=row.get("published_at_interval"))] += 1
    return {"material_count": len(rows), "labeled_count": len(reviewed),
            "unlabeled_count": len(rows) - len(reviewed),
            "label_coverage": round(len(reviewed) / len(rows), 4) if rows else None,
            "direct_user_signal_count": direct,
            "recent_direct_user_signal_count": direct_dates["in_window"],
            "direct_user_publication_window_counts": dict(sorted(direct_dates.items())),
            "direct_user_share_of_labeled": round(direct / len(reviewed), 4) if reviewed else None,
            "supplier_count": sum(label["actor"] == "supplier" for label in reviewed),
            "promotion_count": sum(label["signal"] == "promotion" for label in reviewed),
            "unknown_count": sum(any(label[field] == "unknown" for field in LABELS) for label in reviewed),
            **{field + "_counts": dict(sorted(Counter(label[field] for label in reviewed).items()))
               for field in LABELS}}


def evaluate_retrieval(evidence: list[dict], annotations: dict, *, as_of: str,
                       estimated_cost_usd: float | None = None) -> dict:
    """评估给定材料全集；分组可重叠，不将局部样本推广到平台总体。"""
    cutoff = date.fromisoformat(as_of)
    if not isinstance(evidence, list) or not isinstance(annotations, dict):
        raise ValueError("材料须为数组，标注输入须为对象")
    if annotations.get("schema_version") != "retrieval-labels-1":
        raise ValueError("标注 schema_version 必须是 retrieval-labels-1")
    if not isinstance(annotations.get("labels"), list):
        raise ValueError("标注需要 labels 数组")
    unique = {}
    for row in evidence:
        key = _key(row)
        industries = row.get("industry_ids")
        if industries is not None and (not isinstance(industries, list)
                or any(not isinstance(value, str) or not value.strip() for value in industries)):
            raise ValueError("industry_ids 必须为非空字符串组成的数组")
        if row.get("query") is not None and not isinstance(row["query"], str):
            raise ValueError("query 必须为字符串或 null")
        if row.get("observed_at") and _day(row["observed_at"]) > cutoff:
            raise ValueError("材料观察日期晚于评估日期")
        if key in unique:
            statistical = ("source", "query", "industry_ids", "published_at", "published_at_interval", "observed_at")
            if _body(unique[key]) != _body(row) or any(unique[key].get(k) != row.get(k) for k in statistical):
                raise ValueError("同一修订对应冲突正文或统计元数据")
        unique[key] = row
    if len({key[0] for key in unique}) != len(unique):
        raise ValueError("评估材料须为当前视图，同一对象不能包含多个修订")
    labels = {}
    for label in annotations["labels"]:
        key = _key(label)
        if key not in unique:
            raise ValueError("标注引用不在本次材料全集或修订已改变")
        if key in labels:
            raise ValueError("同一修订不能重复标注")
        for field, allowed in LABELS.items():
            if label.get(field) not in allowed:
                raise ValueError(f"无效标注 {field}")
        for field in ("reviewer", "rationale", "quote"):
            if not isinstance(label.get(field), str) or not label[field].strip():
                raise ValueError(f"标注缺少 {field}")
        reviewed = _day(label.get("reviewed_at"))
        if reviewed > cutoff:
            raise ValueError("不能使用未来标注")
        row = unique[key]
        if row.get("observed_at") and reviewed < _day(row["observed_at"]):
            raise ValueError("标注时间不能早于所引用材料的观察时间")
        if not any(label["quote"] in body for body in _body(row)):
            raise ValueError("标注 quote 必须逐字出现在该修订正文中")
        if label["query_match"] != "unknown" and not str(row.get("query") or "").strip():
            raise ValueError("缺少原始检索 query 的材料必须保留 unknown")
        labels[key] = label
    rows = list(unique.values())
    groups = {"industry": defaultdict(list), "source": defaultdict(list), "query": defaultdict(list)}
    for row in rows:
        for industry in set(row.get("industry_ids") or ["unassigned"]):
            groups["industry"][industry].append(row)
        groups["source"][str(row.get("source") or "unknown")].append(row)
        groups["query"][str(row.get("query") or "unrecorded")].append(row)
    result = {"schema_version": "retrieval-benchmark-1", "as_of": as_of,
              "evaluation_kind": "descriptive_annotated_corpus", "input_count": len(evidence),
              "duplicate_revision_count": len(evidence) - len(rows), "research_window": research_window(as_of),
              "summary": _counts(rows, labels, as_of),
              "groups": {kind: {name: _counts(items, labels, as_of) for name, items in sorted(values.items())}
                         for kind, values in groups.items()},
              "limitations": ["当前材料全集的人工或 Agent 语义标注统计，不是自动分类器准确率。",
                              "分行业可能重叠；评论和来源对象数不等于独立用户数。",
                              "直接用户信号不等于已付费、已验证市场或 AI 增量成立。",
                              "近期按原始发布日期或确定落在窗口内的日期区间计算，观察时间不替代发布日期。",
                              "未标注、未知与未记录原始查询均保留，不从少量检索推断整个平台质量。"]}
    if estimated_cost_usd is not None:
        if isinstance(estimated_cost_usd, bool) or not math.isfinite(estimated_cost_usd) or estimated_cost_usd < 0:
            raise ValueError("估算费用必须为非负有限数值")
        count = result["summary"]["direct_user_signal_count"]
        result["cost"] = {"estimated_usd": estimated_cost_usd, "basis": "caller_supplied_corpus_estimate_not_invoice",
                          "estimated_usd_per_direct_user_signal": round(estimated_cost_usd / count, 6) if count else None}
    return result
