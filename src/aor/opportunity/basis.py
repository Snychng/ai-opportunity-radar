"""为机会判断补充可定位的依据，不自动判断证据语义或调整分数。"""

from __future__ import annotations

from typing import Any, Iterable

from aor.evidence.claims import validate_claims


class OpportunityError(ValueError):
    """机会依据或选择配置不符合契约。"""


def validate_basis(
    value: Any, evidence: list[dict[str, Any]], *, basis_id: str, as_of: str | None = None
) -> dict[str, Any]:
    """核验依据引用；缺失依据返回说明，错误引用则拒绝。"""
    if value is None:
        value = {}
    if not isinstance(value, dict):
        raise OpportunityError(f"{basis_id} 必须是依据对象")
    rationale = value.get("rationale")
    if rationale is not None and not isinstance(rationale, str):
        raise OpportunityError(f"{basis_id}.rationale 必须是字符串")
    rationale = rationale.strip() if rationale else None
    refs = value.get("evidence_refs", [])
    if not isinstance(refs, list):
        raise OpportunityError(f"{basis_id}.evidence_refs 必须是数组")
    located_refs = []
    if refs:
        if not isinstance(evidence, list) or not all(isinstance(item, dict) for item in evidence):
            raise OpportunityError("evidence 必须是对象数组")
        # 旧候选可只有 URL；这些记录继续参与旧门槛，但不能被新 ID 引用寻址。
        addressable = [item for item in evidence if item.get("evidence_id") or item.get("id")]
        try:
            located_refs = validate_claims([{
                "id": basis_id, "statement": rationale or "评分或选择依据尚未填写",
                "evidence_refs": refs, "verification_status": "unverified",
            }], addressable, as_of=as_of)[0]["evidence_refs"]
        except ValueError as exc:
            raise OpportunityError(f"{basis_id} 引用无效：{exc}") from exc
    missing = []
    if not rationale:
        missing.append("rationale")
    if not refs:
        missing.append("evidence_refs")
    return {
        "rationale": rationale, "evidence_refs": located_refs,
        "reference_validation": "located" if refs else "missing",
        "semantic_validation": "not_performed", "missing": missing,
    }


def validate_score_basis(
    basis: Any, evidence: list[dict[str, Any]], fields: Iterable[str], *, as_of: str | None = None
) -> dict[str, Any]:
    """逐维保留评分依据和缺口；旧调用可以不填写 basis。"""
    if basis is None:
        basis = {}
    if not isinstance(basis, dict):
        raise OpportunityError("score_basis 必须是对象")
    fields = tuple(fields)
    unknown = sorted(set(basis) - set(fields))
    if unknown:
        raise OpportunityError(f"未知评分依据维度：{', '.join(unknown)}")
    dimensions = {
        field: validate_basis(basis.get(field), evidence, basis_id=f"score-{field}", as_of=as_of)
        for field in fields
    }
    missing = [field for field, value in dimensions.items() if value["missing"]]
    return {
        "dimensions": dimensions, "missing_dimensions": missing,
        "warnings": [f"{field} 缺少评分依据：{', '.join(dimensions[field]['missing'])}" for field in missing],
        "semantic_validation": "not_performed", "affects_score": False,
    }
