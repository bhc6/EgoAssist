const memoryKinds = {scene: "场景", event: "事件", fact: "事实", state: "状态", preference: "偏好",
  "task-preference": "任务偏好", observation: "观测", "tool-effect": "工具结果", legacy: "旧记录"};
const memoryScopeLabel = value => ({case: "当前会话", participant: "跨会话"}[value] || value || "未记录");
let memoryViewSession = null;
const memoryEventLabels = {
  "episodic.memory.retrieved": "检索完成", "episodic.memory.injected": "证据已注入",
  "episodic.memory.written": "记忆已写入", "response.served.memory_transaction": "记忆事务完成",
  "memory.candidate": "等待记忆确认", "memory_status": "记忆状态更新",
};

function memoryTime(value, timezone) {
  if (!value) return "未记录";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  try { return date.toLocaleString("zh-CN", {timeZone: timezone || undefined, hour12: false}) + (timezone ? " · " + timezone : ""); }
  catch (_) { return value; }
}

function memoryRecords(episodic) {
  const views = episodic.timelineViews || {};
  return [...new Map([...(views.participant?.recent || []), ...(views.case?.recent || []), ...(episodic.recent || [])]
    .map(card => [card.id, card])).values()].sort((a, b) =>
    String(b.happenedAt || b.recordedAt || b.timestamp || "").localeCompare(String(a.happenedAt || a.recordedAt || a.timestamp || "")));
}

function renderMemoryRetrieval(episodic) {
  const audit = episodic.retrieval || {};
  const evidence = audit.injection?.evidence || [];
  const candidates = audit.candidates || [];
  const injectionLabels = {injected: "已交给回答模型", "no-evidence": "没有可用证据", "not-injected": "未交给回答模型", disabled: "记忆已关闭"};
  const stateLabel = injectionLabels[audit.injection?.status] || audit.injection?.status || "尚无检索记录";
  const scope = audit.scope?.memoryScope || audit.scope?.session;
  const html = "<div class='memory-section-heading'><h2>最近一次记忆检索</h2><time>" + esc(memoryTime(audit.timestamp)) + "</time></div>" +
    (audit.query ? "<p class='memory-query'>" + esc(audit.query) + "</p>" : "") +
    "<ol class='memory-flow'><li><span>检索候选</span><b>" + candidates.length + " 条</b></li><li><span>交给回答模型</span><b>" + evidence.length +
    " 条</b></li><li><span>证据状态</span><b>" + esc(stateLabel) + "</b></li></ol>" +
    (evidence.length ? "<ul class='memory-evidence'>" + evidence.map(item => "<li><p>" + esc(item.summary || item.text || "无文本记录") +
      "</p><small>来源 " + esc((item.sourceIds || []).join(" · ") || item.episodeId || item.cardId || "未记录") + "</small></li>").join("") + "</ul>" : "") +
    "<details data-disclosure='retrieval'><summary>检索依据与候选记录</summary>" +
    "<p>检索方式：" + esc(audit.method || "未执行") + (scope ? " · 范围：" + esc(memoryScopeLabel(scope)) : "") + "</p>" +
    candidates.map(item => "<article class='memory-candidate'><b>" + esc(item.selected ? "已选用" : "未选用") + "</b><p>" +
      esc(item.record?.summary || item.record?.text || item.episodeId || "无文本记录") + "</p>" +
      (item.score != null ? "<small>检索排序分数 " + esc(item.score) + "（非事实置信度）</small>" : "") +
      "<p>" + esc([...(item.retrievalReasons || []), ...(item.rejectionReasons || [])].join(" · ")) + "</p></article>").join("") +
    "<pre>" + esc(JSON.stringify(audit, null, 2)) + "</pre></details>";
  updateDisclosureHTML(document.querySelector("#timelineRetrievalSummary"), html, selectedSessionId);
}

function renderMemoryActivity() {
  const root = document.querySelector("#memoryActivity");
  const status = document.querySelector("#memoryActivityStatus");
  if (!root) return;
  const events = (state?.events || []).filter(event => {
    const type = event.type || event.eventType || event.payload?.type;
    return type && (memoryEventLabels[type] || type.includes("memory"));
  }).slice(-12).reverse();
  if (state?.memory?.candidate) {
    events.unshift({
      type: "memory.candidate", eventId: "live-candidate",
      timestamp: new Date().toISOString(), payload: {status: "等待用户确认", turnId: state.memory.candidate.turnId},
    });
  }
  if (status) status.textContent = events.length ? "最近 " + events.length + " 个事件" : "暂无记忆事件";
  const html = events.length ? events.map(event => {
    const type = event.type || event.eventType || event.payload?.type;
    const payload = event.payload || event;
    const turnId = payload.turnId || payload.origin?.turnId;
    const detail = payload.status || payload.reason || payload.storageScope || payload.method || "已记录";
    return "<li><time>" + esc(eventTime(event)) + "</time><b>" + esc(memoryEventLabels[type] || type) +
      "</b><span>" + esc(detail) + (turnId ? " · 对话 " + esc(turnId.slice(0, 8)) : "") +
      "</span><details data-disclosure='activity-" + esc(event.eventId || event.id || type) + "'><summary>原始事件</summary><pre>" +
      esc(JSON.stringify(event, null, 2)) + "</pre></details></li>";
  }).join("") : "<li class='memory-activity-empty'>尚未发生记忆写入或检索</li>";
  updateDisclosureHTML(root, html, selectedSessionId);
}

function memoryCardHTML(card, injected) {
  const evidence = card.evidenceContext || card.metadata?.evidenceContext || {};
  const field = (label, value) => "<dt>" + esc(label) + "</dt><dd>" + esc(value) + "</dd>";
  const sources = card.sourceIds || [];
  return "<details class='memory-record' data-disclosure='" + esc(card.id) + "'><summary><span class='memory-record-meta'><b>" +
    esc(memoryKinds[card.kind] || card.kind || "记录") + "</b><time>" + esc(memoryTime(card.happenedAt || card.recordedAt || card.timestamp, card.timezone)) +
    "</time><span>" + esc(memoryScopeLabel(card.scope)) + "</span>" + (injected ? "<strong>最近检索已选用</strong>" : "") +
    "</span><span class='memory-record-preview'>" + esc(card.text || card.summary || "无文本记录") + "</span></summary>" +
    "<div class='memory-record-body'><h3>保存文本</h3><p class='memory-original'>" + esc(card.text || card.summary || "无文本记录") + "</p><dl>" +
    field("提及对象", (evidence.entities || []).join("、") || "未提取") + field("场景", evidence.scene || "未提取") +
    field("发生时间", memoryTime(card.happenedAt, card.timezone)) + field("记录时间", memoryTime(card.recordedAt || card.timestamp, card.timezone)) +
    field("生效时间", memoryTime(card.effectiveAt, card.timezone)) +
    (card.effectiveEnd ? field("结束时间", memoryTime(card.effectiveEnd, card.timezone)) : "") +
    (evidence.times || []).map(item => field(item.relation || "时间关系", [item.expression, item.start ? memoryTime(item.start, card.timezone) : "时间待解析", item.status === "resolved" ? "已解析" : item.reason || "未解析"].filter(Boolean).join(" · "))).join("") +
    field("来源记录", sources.join(" · ") || "无来源标识") + "</dl>" +
    "<details data-disclosure='raw-" + esc(card.id) + "'><summary>完整数据与来源标识</summary><pre>" + esc(JSON.stringify(card, null, 2)) +
    "</pre></details><button type='button' class='memory-delete' data-delete-card='" + esc(card.id) + "'>删除这条记忆</button></div></details>";
}

function renderTimelineCards(episodic = {}) {
  const root = document.querySelector("#timelineCards");
  if (memoryViewSession !== selectedSessionId) {
    memoryViewSession = selectedSessionId;
    root.replaceChildren();
    document.querySelector("#memorySearch").value = "";
    document.querySelector("#memoryFilterScope").value = "";
  }
  renderMemoryRetrieval(episodic);
  renderMemoryActivity();
  const cards = memoryRecords(episodic);
  const query = document.querySelector("#memorySearch").value.trim().toLocaleLowerCase();
  const scope = document.querySelector("#memoryFilterScope").value;
  const visible = cards.filter(card => (!scope || card.scope === scope) && (!query ||
    JSON.stringify([card.text, card.summary, card.evidenceContext || card.metadata?.evidenceContext]).toLocaleLowerCase().includes(query)));
  document.querySelector("#memoryCount").textContent = "显示 " + visible.length + " / 已加载 " + cards.length + " 条";
  const selected = new Set((episodic.retrieval?.injection?.evidence || []).map(item => item.episodeId || item.cardId));
  // Reconcile by record ID: unrelated updates never replace an expanded record.
  const existing = new Map([...root.children].map(node => [node.dataset.memoryId, node]));
  cards.forEach((card, index) => {
    const node = existing.get(card.id) || document.createElement("article");
    node.dataset.memoryId = card.id;
    updateDisclosureHTML(node, memoryCardHTML(card, selected.has(card.id)), selectedSessionId);
    node.hidden = !visible.includes(card);
    if (root.children[index] !== node) root.insertBefore(node, root.children[index] || null);
    existing.delete(card.id);
  });
  existing.forEach(node => node.remove());
  let empty = root.querySelector(".memory-empty");
  if (!visible.length) {
    if (!empty) { empty = document.createElement("p"); empty.className = "memory-empty"; root.append(empty); }
    empty.textContent = cards.length ? "没有匹配的记忆" : "尚无已保存记忆";
  } else empty?.remove();
}

document.querySelector("#memorySearch").addEventListener("input", () => renderTimelineCards(state?.episodicMemory));
document.querySelector("#memoryFilterScope").addEventListener("change", () => renderTimelineCards(state?.episodicMemory));
document.querySelector("#timelineCards").addEventListener("click", event => {
  const button = event.target.closest("[data-delete-card]");
  if (!button || button.disabled || !confirm("删除这条记忆？原始对话仍保留在账户记录中。")) return;
  runMemoryMutation(button, post("/api/memory/delete", {id: button.dataset.deleteCard, sessionId: selectedSessionId}));
});
