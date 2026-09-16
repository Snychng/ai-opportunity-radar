"""围绕产品、别名和任务生成正负反馈均衡的搜索意图。"""

from contracts import canonical_sha256
from .planning import text_field, validate_intent_plan
from .industries import load_industries

ANGLES = (
    ("experience", "使用体验", "experience", "product_review"),
    ("retention", "一直在用", "worth it", "usage_behavior"),
    ("switching", "替代 退款", "alternative refund", "alternative"),
)


def product_intents(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"products"}:
        raise ValueError("产品输入只包含 products 数组")
    products = payload["products"]
    if not isinstance(products, list) or not 1 <= len(products) <= 10:
        raise ValueError("每轮选择 1–10 个产品，更多产品分轮调查")
    known = {r["id"] for r in load_industries()}
    intents, seen = [], set()
    for product in products:
        if not isinstance(product, dict):
            raise ValueError("每个产品必须为对象")
        name = text_field(product.get("name"), "product.name", 60)
        task = text_field(product.get("task"), "product.task", 150)
        industries = product.get("industry_ids")
        if not isinstance(industries, list) or not industries or not all(isinstance(v, str) and v in known for v in industries):
            raise ValueError("产品需要有效 industry_ids")
        aliases = product.get("aliases", [])
        if not isinstance(aliases, list) or len(aliases) > 2:
            raise ValueError("每个产品最多 2 个搜索别名")
        sources = product.get("sources", ["xiaohongshu", "douyin", "twitter"])
        if not isinstance(sources, list) or not sources or not all(isinstance(s, str) and s in {"xiaohongshu", "douyin", "twitter"} for s in sources):
            raise ValueError("产品来源仅支持小红书、抖音和 X")
        for alias in [name, *[text_field(v, "alias", 60) for v in aliases]]:
            for source in sources:
                for angle, zh, en, role in ANGLES:
                    keyword = f"{alias} {en if source == 'twitter' else zh}"
                    key = (source, keyword)
                    if key in seen:
                        continue
                    seen.add(key)
                    intents.append({"id": "product-" + canonical_sha256(key)[:20], "source": source,
                        "question": f"{name} 的用户在 {task} 中实际体验如何？角度：{angle}",
                        "evidence_type": role, "search_query": keyword, "ranking_query": f"{name} {task}",
                        "locale": {"country": "unknown" if source == "twitter" else "CN",
                                   "language": "en" if source == "twitter" else "zh"},
                        "industry_ids": industries, "candidate_gaps": ["user_experience", "product_context"]})
    if len(intents) > 100:
        raise ValueError("产品别名与来源组合超过 100 个查询，请减少产品或别名并分轮执行")
    return validate_intent_plan({"intents": intents})
