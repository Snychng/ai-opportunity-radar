// 只消费公开契约；新增字段和未知枚举保守展示，原文通过 textContent 写入。
const $ = (id) => document.getElementById(id);
const state = { data: null, industry: "all", selected: null };
const stages = { hypothesis: "待验证假设", observed_need: "观察到需求", candidate: "研究候选", regional_hypothesis: "迁移假设" };
const freshnessLabels = { fresh: "复核有效期内", due: "即将到期", overdue: "待重新复核", unknown: "复核日期不足" };
function el(tag, text, className) { const node = document.createElement(tag); if (text != null) node.textContent = text; if (className) node.className = className; return node; }
function tag(text, caution = false) { return el("span", text, `tag${caution ? " caution" : ""}`); }
function industryName(id) { return state.data.industries.find((x) => x.id === id)?.name || "其他方向"; }
function visibleItems() {
  const query = $("search").value.trim().toLocaleLowerCase();
  return state.data.items.filter((x) => ($("status").value === "all" || x.publication_status === "published") && (state.industry === "all" || x.industry_ids.includes(state.industry)) && (!query || [x.title, x.summary, x.target_user, x.job_to_be_done].join(" ").toLocaleLowerCase().includes(query)));
}
function render() {
  const items = visibleItems();
  $("results-status").textContent = `${state.industry === "all" ? "全部方向" : industryName(state.industry)} · ${items.length} 条线索`;
  const coverage = state.data.coverage?.find((x) => x.industry_id === state.industry);
  $("coverage-note").textContent = coverage ? `该方向本轮新增 ${coverage.material_count} 条材料，另复用 ${coverage.historical_material_count ?? "未知数量的"} 条历史材料。本轮新增材料已审 ${coverage.reviewed_count} 条；历史复核计入顶部全站进度。材料数不等于机会数，子赛道仍会继续补充。` : "选择一个方向，查看材料复核进度。目录展示已完成发布检查的线索，研究覆盖仍在扩充。";
  $("items").replaceChildren();
  if (!items.length) $("items").append(el("p", "当前筛选下暂无可展示线索。该方向仍在研究，不能由此判断没有机会。", "empty"));
  for (const item of items) {
    const article = el("article", null, `opportunity${state.selected === item.id ? " selected" : ""}`);
    const meta = el("div", null, "meta");
    item.industry_ids.forEach((id) => meta.append(tag(industryName(id))));
    meta.append(tag(item.publication_status === "published" ? (stages[item.stage] || "研究线索") : "已归档 / 暂不展示", item.publication_status !== "published"));
    const status = item.freshness?.status || "unknown";
    meta.append(tag(freshnessLabels[status] || freshnessLabels.unknown, status !== "fresh"));
    const title = el("h3"); const button = el("button", item.title, "open-detail");
    button.dataset.item = item.id;
    button.addEventListener("click", () => openDetail(item.id)); title.append(button);
    article.append(meta, title, el("p", item.summary), el("div", `下一步：${item.next_action || "补充验证计划"}`, "next"));
    $("items").append(article);
  }
  for (const button of $("industries").children) button.setAttribute("aria-pressed", String(button.dataset.industry === state.industry));
}
function section(container, title, text) { container.append(el("h3", title), el("p", text || "尚待补充")); }
function openDetail(id) {
  const item = state.data.items.find((x) => x.id === id); if (!item) return;
  state.selected = id; render(); const content = $("detail-content"); content.replaceChildren();
  content.append(el("span", item.publication_status === "published" ? "机会线索 · 尚需验证" : "历史记录 · 已归档", "eyebrow"));
  const title = el("h2", item.title); title.id = "detail-title"; content.append(title);
  section(content, "为谁解决什么问题", `${item.target_user}\n${item.job_to_be_done}`);
  section(content, "可以切入的位置", item.wedge);
  section(content, "今天怎么做", item.ai_value?.baseline);
  section(content, "AI 可能带来的变化", item.ai_value?.incremental_advantage);
  section(content, "先验证这一步", item.next_action);
  content.append(el("h3", "还缺哪些依据")); const missing = el("ul");
  (item.missing_evidence || []).forEach((x) => missing.append(el("li", x))); content.append(missing);
  const fresh = item.freshness || {};
  section(content, "复核状态", `${freshnessLabels[fresh.status] || freshnessLabels.unknown}。上次核验：${fresh.last_verified_at || "未知"}；下次复核：${fresh.review_due_at || "待确定"}。`);
  content.append(el("h3", "可追溯来源"));
  for (const ref of item.evidence_refs || []) {
    const evidence = state.data.evidence.find((x) => x.id === ref.id && x.revision_id === ref.revision_id);
    if (!evidence) continue;
    const block = el("blockquote"); block.append(el("p", ref.quote || evidence.quote || evidence.excerpt || evidence.title));
    try { const url = new URL(evidence.url); if (url.protocol === "https:" && !url.username && !url.password) { const link = el("a", evidence.title || "查看原始来源"); link.href = url.href; link.target = "_blank"; link.rel = "noopener noreferrer"; block.append(link); } } catch { /* 缺失或非公开链接不渲染。 */ }
    content.append(block);
  }
  if (!item.evidence_refs?.length) content.append(el("p", "当前记录不提供可用于现行结论的引用。"));
  const location = new URL(window.location); location.searchParams.set("item", id); history.replaceState(null, "", location);
  if (!$("detail").open) $("detail").showModal(); $("close").focus();
}
$("close").addEventListener("click", () => $("detail").close());
$("detail").addEventListener("click", (event) => { const rect = $("detail").getBoundingClientRect(); if (event.target === $("detail") && (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom)) $("detail").close(); });
$("detail").addEventListener("close", () => { const url = new URL(window.location); url.searchParams.delete("item"); history.replaceState(null, "", url); [...document.querySelectorAll(".open-detail")].find((x) => x.dataset.item === state.selected)?.focus(); });
$("search").addEventListener("input", render); $("status").addEventListener("change", render);
try {
  const response = await fetch("./public.v1.json", { cache: "no-cache" }); if (!response.ok) throw new Error("公开数据尚未载入");
  const data = await response.json();
  if (!/^1\./.test(data.contract_version) || data.view !== "full" || !Array.isArray(data.items) || !Array.isArray(data.industries) || !Array.isArray(data.evidence)) throw new Error("数据契约不兼容，需要更新消费端");
  state.data = data;
  const published = data.items.filter((x) => x.publication_status === "published").length;
  const review = data.quality?.review_summary;
  for (const [number, label] of [[data.industries.length, "研究方向"], [published, "当前线索"], [review ? `${review.reviewed_count}/${review.evidence_count}` : "未知", "材料已复核"], [data.as_of, "数据截止日"]]) { const metric = el("div", null, "metric"); metric.append(el("strong", number), el("span", label)); $("overview").append(metric); }
  for (const industry of [{ id: "all", name: "全部方向" }, ...data.industries]) {
    const button = el("button"); button.dataset.industry = industry.id;
    const count = data.items.filter((x) => x.publication_status === "published" && (industry.id === "all" || x.industry_ids.includes(industry.id))).length;
    button.append(el("span", industry.name), el("small", count)); button.addEventListener("click", () => { state.industry = industry.id; render(); }); $("industries").append(button);
  }
  render(); const id = new URL(window.location).searchParams.get("item"); if (id) openDetail(id);
} catch (error) { $("results-status").textContent = `无法展示：${error.message}。请通过本地 HTTP 服务打开，并先放入校验通过的 public.v1.json。`; }
