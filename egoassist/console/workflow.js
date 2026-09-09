/* Durable turn state is the source of truth, including after a page reload. */
let inspectedTurnId = null;
const workStatusLabel = value => ({received: "已接收", classifying: "分析请求与说话人", ready: "等待执行",
  processing: "处理请求", responding: "正在回答", completed: "已完成", queued: "等待执行", running: "执行中",
  "waiting-result": "等待外部结果", suspended: "已挂起", paused: "已暂停", cancelled: "已取消",
  interrupted: "播报被打断", failed: "失败", delivered: "已送达", dismissed: "已确认", pending: "待处理",
  available: "可用", unsupported: "暂不支持", unconfigured: "未配置", foreground: "语音回答中"}[value] || value || "尚无状态");
const workKindLabel = value => ({"foreground-dialogue": "回答问题", "background-task": "后台任务",
  "procedural-task": "步骤任务", "timer": "定时提醒", "tool-call": "工具执行", "memory-query": "查找记忆",
  other: "对别人说", assistant: "对助手说", uncertain: "尚不明确", listen: "只记录，不插话", reply: "需要回答",
  resume: "继续回答", cancel: "取消回答", pause: "暂停回答"}[value] || value || "任务");

function renderTurnWorkflow() {
  const root = document.querySelector("#liveWorkflow");
  if (!root) return;
  const turns = state?.workLedger?.turnInbox || [];
  const turn = turns.find(t => t.turnId === inspectedTurnId) || turns.at(-1);
  if (!turn) { root.innerHTML = "<p class='empty-state'>尚未收到问题</p>"; return; }
  const scene = state?.sharedScene?.utterances?.find(row => row.turnId === turn.turnId);
  const answer = turn.answer, speaker = scene?.speaker;
  const semantic = turn.semantic;
  const pendingVision = state.sharedScene?.questionVisionPending?.find(p => p.turnId === turn.turnId);
  const events = (state.events || []).filter(e => e.payload?.turnId === turn.turnId ||
    e.payload?.origin?.turnId === turn.turnId || (answer?.responseId && e.responseId === answer.responseId));
  const elapsed = answer?.startedAt && answer.status === "running" ?
    " · 已等待 " + Math.max(0, (Date.now() - Date.parse(answer.startedAt)) / 1000).toFixed(1) + " 秒 / 上限 " + answer.timeoutSeconds + " 秒" : "";
  const row = (label, detail) => "<div class='workflow-fact'><b>" + esc(label) + "</b><span>" + esc(detail) + "</span></div>";
  root.innerHTML = "<label class='workflow-select'>查看问题<select id='workflowTurnSelect'><option value=''>跟随最新问题</option>" +
    turns.map(t => "<option value='" + esc(t.turnId) + "'" + (t.turnId === inspectedTurnId ? " selected" : "") + ">" + esc(t.text) + "</option>").join("") +
    "</select></label><blockquote>" + esc(turn.text) + "</blockquote>" +
    row("当前进度", (!answer && pendingVision && turn.status !== "completed" ? "等待画面描述 · " + pendingVision.model + " · " + pendingVision.elapsedMs + " ms" : !answer && turn.status === "completed" && semantic?.interaction?.action === "listen" ? "现场对话已记录，无需助手回答" : workStatusLabel(answer?.status || turn.delivery?.status || turn.status)) + elapsed) +
    row("说话人", speaker?.status === "account-input" ? "账号提交的文字" : speaker?.status === "pending" ? "本地模型正在分析声音" : speaker?.name || speaker?.speakerId || "现场声音，身份未确认") +
    row("声画证据", (scene?.audio ? "原始语音已保留" : "无对应语音片段") + " · " +
      (scene?.frame ? "已绑定提问时画面 " + scene.frame.receivedAt : "无可对齐画面") +
      (scene?.synchronizedFrames?.length ? " · " + scene.synchronizedFrames.length + " 帧按接收时间对齐" : "")) +
    row("人物关联", scene?.avAssociation?.status === "account-confirmed" ? "账号已确认参考画面关联" : "声音与画面人物尚未确认关联") +
    row("请求判断", semantic ? [semantic.interaction?.audience, semantic.interaction?.action,
      semantic.selectedSkill, semantic.selectedTask].filter(Boolean).map(workKindLabel).join(" · ") || "已完成分类" : "等待分类记录") +
    row("记忆与权限", semantic?.speakerRestricted ? "身份未确认：禁止个人记忆和任务变更" :
      semantic?.memoryQuery ? "本轮请求检索记忆" : semantic?.memoryText ? "本轮包含记忆候选" : "本轮没有显式记忆请求") +
    row("回答模型", answer?.model || "尚无该问题的回答模型记录") +
    row("分类耗时", semantic?.latencyMs != null ? semantic.latencyMs + " ms" : "尚未完成测量") +
    row("接收到首字", answer?.inputToFirstTextMs != null ? answer.inputToFirstTextMs + " ms" : "尚未收到文字") +
    row("接收到首音频", answer?.inputToFirstAudioMs != null ? answer.inputToFirstAudioMs + " ms" : "尚未收到音频") +
    row("接收到完整回答", answer?.inputToCompleteMs != null ? answer.inputToCompleteMs + " ms" : "尚未完成") +
    row("回答耗时", answer?.durationMs != null ? answer.durationMs + " ms" : answer?.generationMs != null ? answer.generationMs + " ms（响应创建至完成）" : "尚未完成测量") +
    (answer?.error ? row("失败原因", answer.error) : "") +
    (semantic ? "<details><summary>分类结果与依据</summary><pre>" + esc(JSON.stringify(semantic, null, 2)) + "</pre></details>" : "") +
    "<details><summary>本问题运行记录（" + events.length + "）</summary><ol class='workflow-event-list'>" +
    events.map(e => "<li><time>" + esc(eventTime(e)) + "</time> <button type='button' data-workflow-event='" + esc(e.eventId) + "'>" + esc(e.type) +
      "</button>" + (e.latencyMs != null ? " · " + esc(e.latencyMs) + " ms" : "") + "</li>").join("") +
    "</ol></details>";
  root.querySelector("select").onchange = event => { inspectedTurnId = event.target.value || null; renderTurnWorkflow(); };
  root.querySelectorAll("[data-workflow-event]").forEach(button => button.onclick = () => {
    const event = events.find(e => e.eventId === button.dataset.workflowEvent);
    if (event) { document.querySelector(".inspector-panel").open = true; inspect(event); }
  });
}

function readableResult(content) {
  if (typeof content === "string") return content;
  if (!content) return "结果已记录";
  for (const key of ["text", "message", "summary", "answer", "label", "title"]) {
    if (typeof content[key] === "string") return content[key];
  }
  if (content.artifact?.title) return content.artifact.title;
  if (content.result && content.result !== content) return readableResult(content.result);
  return "处理结果已保存，详情中可查看原始记录";
}
