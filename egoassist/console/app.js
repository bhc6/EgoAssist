const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
let state, stream, socket, audioContext, microphoneSource, processor, videoTimer;
let playbackTime = 0, assistantText = "";
let connectingPromise = null;
let connectionGeneration = 0;
let mediaPromise = null;
let mediaGeneration = 0;
let selectedEventId = null, pipelineDetails = [];
let currentUser = null, selectedSessionId = null, authMode = "login";
let sessionCatalog = [], memoryPolicySaving = false;
let mirrorCamera = localStorage.getItem("egoassist.camera.mirror") !== "false";
const playbackSources = new Set();
const AUDIO_BUFFER_HIGH_WATER = 512 * 1024;
const VIDEO_BUFFER_HIGH_WATER = 128 * 1024;
let droppedAudioChunks = 0;
let droppedVideoFrames = 0;

async function request(path, options = {}) {
  const response = await fetch(path, options);
  const body = await response.text();
  let result;
  try { result = JSON.parse(body); }
  catch (_) { throw Error((response.ok ? "服务返回了无效 JSON：" : "请求失败：") + body.slice(0, 180)); }
  if (response.status === 401) {
    showLogin();
    throw Error("登录状态已失效");
  }
  if (!response.ok) throw Error(result.message || result.error || "请求失败");
  return result;
}

async function post(path, data = {}) {
  return request(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
}

const selectedStatePath = () => "/api/state" + (selectedSessionId ? "?session=" + encodeURIComponent(selectedSessionId) : "");

function setAuthMode(mode) {
  authMode = mode;
  const registering = mode === "register";
  $("#loginMode").classList.toggle("active", !registering);
  $("#registerMode").classList.toggle("active", registering);
  $("#authTitle").textContent = registering ? "注册参与者账号" : "登录参与者账号";
  $("#authSubmit").textContent = registering ? "注册并进入" : "登录";
  $("#authPassword").autocomplete = registering ? "new-password" : "current-password";
  $("#authError").textContent = "";
}

function showLogin() {
  disconnectRealtime();
  currentUser = null;
  selectedSessionId = null;
  document.body.classList.add("auth-required");
  $("#authGate").hidden = false;
  $("#authPassword").value = "";
}

async function refreshSessionList(preferredId = selectedSessionId) {
  const payload = await request("/api/sessions");
  let sessions = payload.sessions || [];
  if (!sessions.length) {
    const created = await post("/api/sessions");
    preferredId = created.session.id;
    sessions = (await request("/api/sessions")).sessions || [];
  }
  selectedSessionId = sessions.some(item => item.id === preferredId) ? preferredId : sessions[0].id;
  sessionCatalog = sessions;
  $("#sessionSelect").innerHTML = sessions.map(item => {
    const seen = item.last_seen_at ? item.last_seen_at.replace("T", " ").slice(0, 19) : "";
    const label = item.label || "未命名案例";
    return "<option value='" + esc(item.id) + "'>" + esc(label + " · " + item.id + (seen ? " · " + seen : "")) + "</option>";
  }).join("");
  $("#sessionSelect").value = selectedSessionId;
  const selected = sessions.find(item => item.id === selectedSessionId);
  if (selected?.memoryPolicy) renderMemoryPolicy(selected.memoryPolicy, state?.memoryProfiles || []);
}

function renderMemoryPolicy(policy, profiles = []) {
  if (!policy) return;
  const profileOptions = [...profiles];
  if (policy.profile === "custom" && !profileOptions.some(item => item.id === "custom")) {
    profileOptions.push({id: "custom", label: "自定义组合"});
  }
  $("#memoryProfile").innerHTML = profileOptions.map(item =>
    "<option value='" + esc(item.id) + "'>" + esc(item.label) + "</option>"
  ).join("");
  $("#memoryProfile").value = policy.profile;
  const preference = policy.components.preference;
  const episodic = policy.components.episodic;
  $("#preferenceEnabled").checked = preference.enabled;
  $("#preferenceScope").value = preference.scope;
  $("#preferenceScope").disabled = !preference.enabled || memoryPolicySaving;
  $("#episodicEnabled").checked = episodic.enabled;
  $("#episodicScope").value = episodic.scope;
  $("#episodicScope").disabled = !episodic.enabled || memoryPolicySaving;
  $("#memoryProfile").disabled = memoryPolicySaving;
  $("#preferenceEnabled").disabled = memoryPolicySaving;
  $("#episodicEnabled").disabled = memoryPolicySaving;
  $("#memoryPolicyStatus").textContent = memoryPolicySaving
    ? "正在保存实验配置…"
    : "policy v" + policy.version + " · context journal 始终用于恢复与审计 · 变更从后续轮次生效";
}

function customMemoryPolicy() {
  return {
    profile: "custom",
    components: {
      preference: {enabled: $("#preferenceEnabled").checked, scope: $("#preferenceScope").value},
      episodic: {enabled: $("#episodicEnabled").checked, scope: $("#episodicScope").value},
    },
  };
}

async function saveMemoryPolicy(policy) {
  if (!selectedSessionId || memoryPolicySaving) return;
  const before = state?.memoryPolicy || sessionCatalog.find(item => item.id === selectedSessionId)?.memoryPolicy;
  memoryPolicySaving = true;
  renderMemoryPolicy(before, state?.memoryProfiles || []);
  try {
    const result = await post("/api/sessions/memory-policy", {sessionId: selectedSessionId, policy});
    const session = sessionCatalog.find(item => item.id === selectedSessionId);
    if (session) session.memoryPolicy = result.memoryPolicy;
    if (state) state.memoryPolicy = result.memoryPolicy;
    renderMemoryPolicy(result.memoryPolicy, result.profiles || state?.memoryProfiles || []);
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: "refresh"}));
  } catch (error) {
    alert(error.message);
  } finally {
    memoryPolicySaving = false;
    const current = state?.memoryPolicy || sessionCatalog.find(item => item.id === selectedSessionId)?.memoryPolicy;
    renderMemoryPolicy(current, state?.memoryProfiles || []);
  }
}

async function enterConsole(user) {
  currentUser = user;
  document.body.classList.remove("auth-required");
  $("#authGate").hidden = true;
  $("#currentUser").textContent = user.username;
  await refreshSessionList(localStorage.getItem("egoassist.session." + user.id));
  localStorage.setItem("egoassist.session." + user.id, selectedSessionId);
  render(await request(selectedStatePath()));
  const selected = sessionCatalog.find(item => item.id === selectedSessionId);
  if (selected?.memoryPolicy) renderMemoryPolicy(selected.memoryPolicy, state.memoryProfiles || []);
}

async function boot() {
  const payload = await request("/api/auth/me");
  if (payload.user) await enterConsole(payload.user);
  else showLogin();
}

const actionName = action => ({silent: "静默", guide: "指导", ask: "询问", correct: "纠错", emergency: "紧急"}[action] || action);
const memoryStageName = key => ({evidence: "证据", candidate: "候选", confirmation: "确认", retrieval: "召回", impact: "影响"}[key] || key);
const memoryStatusName = status => ({
  observed: "已有证据", waiting: "等待中", pending: "待确认", confirmed: "已确认",
  matched: "已匹配", ready: "待语音提醒", reused: "已注入", "already-used": "已抑制重复",
  "no-match": "无匹配", none: "无", disabled: "已关闭"
}[status] || status);

const eventClass = type => type.startsWith("media.") || type.startsWith("audio.") || type.startsWith("perception.") ? "input" : type.startsWith("memory.") ? "memory" : type.startsWith("response.") || type.startsWith("barge_in.") ? "response" : type.includes("decision") || type.startsWith("task.") ? "decision" : "";
const eventTime = event => (event.timestampWall || "").slice(11, 23);

function inspect(value) {
  const event = value && value.eventId ? value : null;
  if (event) selectedEventId = event.eventId;
  const title = event ? event.type : value.title;
  const metadata = event ? {
    "event id": event.eventId, "parent ids": (event.parentEventIds || []).join(", ") || "root",
    source: event.source, time: event.timestampWall, model: event.model || "-",
    skill: event.skill || "-", task: event.task || "-", "response id": event.responseId || "-",
    latency: event.latencyMs == null ? "-" : event.latencyMs + " ms"
  } : {stage: value.stage, status: value.status, claim: value.claim || "-"};
  $("#eventInspector").innerHTML = "<div class='event-summary'><span>SELECTED OBSERVABLE</span><h3>" + esc(title) + "</h3><div class='meta-grid'>" + Object.entries(metadata).map(([key, item]) => "<span>" + esc(key) + "</span><b>" + esc(item) + "</b>").join("") + "</div></div><pre>" + esc(JSON.stringify(event || value, null, 2)) + "</pre>";
}

function renderPipeline(task, mechanism, step) {
  const scheduler = state.scheduler || {};
  const lanes = state.providerLanes || {};
  const retrieval = mechanism.retrieval || {};
  const routing = state.intentRouting || {status: "no-match", candidates: []};
  const decision = task?.decision || state.genericDecision || {action: "silent", confidence: 0, reason: "no-domain-intent"};
  const taskSummary = task && step ? step.title + " · " + step.status : state.activeSkill ? "Skill loaded · task unresolved" : "No task hypothesis";
  pipelineDetails = [
    {stage: "capture", title: "音频 / 视频采集", status: stream ? "live" : "idle", summary: stream ? "16 kHz PCM + latest-only JPEG" : "等待浏览器设备", claim: "原始媒体只记录大小和时间，不在账本保存 Base64"},
    {stage: "attribution", title: "声道与说话者归因", status: stream ? "live" : "idle", summary: "input transcript ≠ spoken output transcript", claim: state.audioAttribution?.claimBoundary || "等待媒体配置"},
    {stage: "intent", title: "意图路由", status: routing.status === "matched" ? "live" : routing.status === "ambiguous" ? "warning" : "idle", summary: routing.status + " · " + (routing.selectedSkill || "general"), claim: routing.claimBoundary},
    {stage: "perception", title: "结构化感知", status: lanes.perception?.connected ? "live" : "idle", summary: lanes.perception?.connected ? "独立视觉门控通道 · 不共享麦克风" : "感知通道未连接", claim: "视觉门控与用户对话使用不同 provider session；模型视觉上下文与已提交控制事件分开展示"},
    {stage: "task", title: "Skill / 任务假设", status: task ? "live" : state.activeSkill ? "warning" : "idle", summary: taskSummary, claim: task ? "Checkpoint ledger，不规定唯一执行路径" : "无证据时不加载领域工作流"},
    {stage: "knowledge", title: "知识增强", status: state.knowledgeMechanism.method === "none" ? "idle" : "warning", summary: state.knowledgeMechanism.method, claim: state.knowledgeMechanism.claimBoundary},
    {stage: "memory", title: "记忆检索", status: retrieval.status === "matched" || state.episodicMemory?.count ? "live" : "idle", summary: "preference " + (retrieval.candidates || []).length + " · episodes " + (state.episodicMemory?.count || 0), claim: "偏好与情景记忆分开；显示查询、候选、拒绝原因和实际注入结果"},
    {stage: "policy", title: "主动干预策略", status: "live", summary: actionName(decision.action) + " · " + Math.round((decision.confidence || 0) * 100) + "%", claim: decision.reason},
    {stage: "scheduler", title: "响应调度与打断", status: scheduler.pending || scheduler.activeResponseIds?.length ? "live" : "idle", summary: (lanes.dialogue?.turnDetection || "unknown") + " · " + (scheduler.activePolicies?.[0]?.speechPolicy || state.turnTakingPolicy.currentSelection.policy), claim: "转写后先检索与更新上下文，再显式创建唯一回答；Skill 可配置：" + state.turnTakingPolicy.currentSelection.basis},
    {stage: "output", title: "语音与字幕输出", status: scheduler.activeResponseIds?.length ? "live" : "idle", summary: scheduler.activeResponseIds?.join(", ") || "等待输出", claim: "只展示可验证输出和结构化中间产物，不声称访问隐藏思维链"}
  ];
  $("#pipeline").innerHTML = pipelineDetails.map((item, index) => "<article class='pipeline-node " + esc(item.status) + "' data-pipeline='" + index + "'><div><h3>" + esc(item.title) + "</h3><p>" + esc(item.summary) + "</p></div><b>" + esc(item.stage) + "</b></article>").join("");
  $$('[data-pipeline]').forEach(node => node.onclick = () => inspect(pipelineDetails[Number(node.dataset.pipeline)]));
}

function renderTimeline(events) {
  $("#eventCount").textContent = events.length + " events";
  $("#timeline").innerHTML = events.length ? events.slice(-60).map(event => "<button class='event-chip " + eventClass(event.type) + "' data-event='" + esc(event.eventId) + "'><b>" + esc(event.type) + "</b><span>" + esc(eventTime(event) + " · " + event.source) + "</span></button>").join("") : "<span>等待事件</span>";
  $$('[data-event]').forEach(node => node.onclick = () => inspect(events.find(event => event.eventId === node.dataset.event)));
  const selected = events.find(event => event.eventId === selectedEventId) || [...events].reverse().find(event => !event.type.startsWith("media."));
  if (selected) inspect(selected);
}

const terminalWorkStatus = status => ["completed", "failed", "cancelled", "delivered", "dismissed"].includes(status);

function workLabel(item, fallback) {
  const semantic = item.payload?.semantic || {};
  return semantic.summary || semantic.input || item.payload?.text || item.skillRef?.task || fallback;
}

function renderWorkCenter() {
  const ledger = state.workLedger || {intents: [], jobs: [], resultInbox: [], foregroundIntentId: null};
  const capabilities = state.capabilityCatalog?.platform || [];
  const unavailable = capabilities.filter(item => item.status !== "available");
  const timerCapability = capabilities.find(item => item.id === "timer-delivery") || {
    status: "unsupported", reason: "Temporal reminder service is not configured.",
  };
  $("#createReminder").disabled = timerCapability.status !== "available";
  $("#reminderAvailability").textContent = timerCapability.status === "available"
    ? "Temporal · durable"
    : timerCapability.status + " · " + timerCapability.reason;
  $("#capabilityTruth").innerHTML = capabilities.map(item =>
    "<span class='capability-state " + esc(item.status) + "' title='" + esc(item.reason) + "'><b>" +
    esc(item.label + " · " + item.status) + "</b></span>"
  ).join("") || "<span class='muted'>能力真值尚未加载</span>";

  const activeJobs = ledger.jobs.filter(item => !terminalWorkStatus(item.status));
  const pendingResults = ledger.resultInbox.filter(item => !["delivered", "dismissed"].includes(item.status));
  $("#workCenterCounts").textContent = activeJobs.length + " active · " + pendingResults.length + " results · " + unavailable.length + " limited";

  $("#intentList").innerHTML = ledger.intents.length ? [...ledger.intents].reverse().map(item => {
    const focused = item.intentId === ledger.foregroundIntentId;
    const canFocus = !focused && !terminalWorkStatus(item.status) && item.detail?.runtimeCheckpoint;
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(workLabel(item, item.kind)) +
      "</b><small>" + esc(item.kind + " · " + item.status) + "</small></div>" +
      (canFocus ? "<button data-focus-intent='" + esc(item.intentId) + "'>切到前台</button>" : focused ? "<b>前台</b>" : "") +
      "</article>";
  }).join("") : "<p class='empty-state'>暂无已记录意图</p>";

  $("#jobList").innerHTML = ledger.jobs.length ? [...ledger.jobs].reverse().map(item => {
    const cancellable = !terminalWorkStatus(item.status);
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(workLabel(item, item.kind)) +
      "</b><small>" + esc(item.kind + " · " + item.status) + "</small></div>" +
      (cancellable ? "<button class='danger' data-cancel-job='" + esc(item.jobId) + "'>取消</button>" : "") +
      "</article>";
  }).join("") : "<p class='empty-state'>暂无可执行任务</p>";

  $("#resultInbox").innerHTML = ledger.resultInbox.length ? [...ledger.resultInbox].reverse().map(item => {
    const content = typeof item.content === "string" ? item.content : JSON.stringify(item.content);
    const dismissible = !["delivered", "dismissed"].includes(item.status);
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(content) +
      "</b><small>" + esc(item.status + " · priority " + item.priority) + "</small></div>" +
      (dismissible ? "<button data-dismiss-result='" + esc(item.resultId) + "'>确认</button>" : "") +
      "</article>";
  }).join("") : "<p class='empty-state'>暂无待处理结果</p>";

  $$('[data-focus-intent]').forEach(button => button.onclick = () => run(button, post("/api/intents/focus", {
    sessionId: selectedSessionId, intentId: button.dataset.focusIntent,
  }).then(payload => payload.state || payload)));
  $$('[data-cancel-job]').forEach(button => button.onclick = () => run(button, post("/api/jobs/cancel", {
    sessionId: selectedSessionId, jobId: button.dataset.cancelJob,
  }).then(payload => payload.state || payload)));
  $$('[data-dismiss-result]').forEach(button => button.onclick = () => run(button, post("/api/results/dismiss", {
    sessionId: selectedSessionId, resultId: button.dataset.dismissResult,
  }).then(payload => payload.state || payload)));
}

function render(next) {
  state = next;
  const task = state.task, memory = state.memory, mechanism = state.memoryMechanism;
  const decision = task?.decision || state.genericDecision;
  const step = task ? task.steps.find(item => item.id === task.currentCheckpointId) || task.steps[task.stepIndex] : null;
  $("#mode").textContent = "● " + state.realtimeModel;
  $("#sessionId").textContent = state.sessionId;
  $("#promptHash").textContent = state.promptContext.hash;
  $("#promptContext").textContent = state.promptContext.text;
  $("#skillName").textContent = state.activeSkill ? state.activeSkill.name + " / " + (task ? task.task.title : "待确认任务") : "General / no Skill";
  $("#taskTitle").textContent = task ? task.task.title + " · " + task.task.version : "No task hypothesis";
  $("#skillSelect").innerHTML = "<option value=''>通用模式 · 按意图加载</option>" + state.skills.map(item => "<option value='" + esc(item.name) + "'>" + esc(item.name) + " · " + item.tasks.length + " tasks</option>").join("");
  $("#skillSelect").value = state.activeSkill?.name || "";
  $("#taskSelect").innerHTML = state.tasks.length ? "<option value=''>选择具体任务</option>" + state.tasks.map(item => "<option value='" + esc(item.id) + "'>" + esc(item.title) + " · " + esc(item.provenance) + "</option>").join("") : "<option value=''>无活动任务</option>";
  $("#taskSelect").value = task?.task.id || "";
  $("#taskSelect").disabled = !state.activeSkill;
  $("#newTask").disabled = !state.activeSkill?.allowCustomTasks;
  $("#skillTools").innerHTML = (state.activeSkill?.tools || []).map(item => {
    const availability = item.availability || {status: "unconfigured", executable: false, reason: "No executor is configured."};
    return "<button data-tool='" + esc(item.name) + "' data-confirm='" + item.requires_confirmation + "' title='" +
      esc(availability.reason) + "' " + (availability.executable ? "" : "disabled") + ">" + esc(item.name) +
      " · " + esc(availability.status) + (item.requires_confirmation && availability.executable ? " · 需确认" : "") + "</button>";
  }).join("") || "<span class='muted'>无领域工具</span>";
  $$("[data-tool]").forEach(button => button.onclick = () => executeTool(button));
  $("#action").textContent = actionName(decision.action);
  $("#decision").className = "decision " + decision.action;
  $("#decision").innerHTML = "<h3>" + esc(decision.message) + "</h3><p>" + esc(decision.reason) + " · confidence " + Math.round(decision.confidence * 100) + "%</p>";
  $("#mapStatus").textContent = task ? "当前假设 " + step.id + " · " + task.steps.length + " checkpoints" : "等待意图与任务证据";
  $("#steps").innerHTML = task ? task.steps.map(item => {
    const label = item.id === task.currentCheckpointId ? "当前系统假设" : "可选证据锚点";
    return "<article class='checkpoint " + esc(item.status) + "'><span>" + esc(item.status) + "<br>confidence " + Math.round((item.confidence || 0) * 100) + "%</span><div><h3>" + esc(item.title) + "</h3><p>" + esc(label + " · " + item.instruction) + "</p></div><b>evidence " + (item.evidenceIds || []).length + "</b></article>";
  }).join("") : "<p class='empty-state'>尚未建立任务假设。意图路由匹配后才加载领域工作流。</p>";
  $("#knowledge").innerHTML = "<b>" + esc(state.knowledgeMechanism.method) + "</b><br>selected: " + esc(step ? step.id + " / " + step.source : "none") + "<br><span>边界：" + esc(state.knowledgeMechanism.claimBoundary) + "</span>";
  $("#vision").textContent = task?.observation ? task.observation.summary + " · " + Math.round(task.observation.confidence * 100) + "% · " + task.observation.source : "无结构化视觉观测；模型视觉上下文不会伪装为已提交事件";
  $("#memoryScope").textContent = "exact-trigger-lookup · " + (mechanism.scope.skill || "general") + " / " + (mechanism.scope.task || "unscoped");
  renderMemoryPolicy(state.memoryPolicy, state.memoryProfiles || []);
  $("#memorySystems").innerHTML = state.memorySystems.map(item => "<article class='system-card " + esc(item.status) + "'><b>" + esc(item.name) + "</b><span>" + esc(item.status + " · " + item.implementation) + "</span></article>").join("");
  $("#memoryFlow").innerHTML = ["evidence", "candidate", "confirmation", "retrieval", "impact"].map(key => {
    const stage = mechanism[key];
    const source = key === "evidence" && stage.source ? "<small>" + esc(stage.source) + "</small>" : "";
    return "<article class='memory-stage " + esc(stage.status) + "'><span>" + esc(memoryStageName(key)) + "</span><b>" + esc(memoryStatusName(stage.status)) + "</b><p>" + esc(stage.text) + "</p>" + source + "</article>";
  }).join("");
  const candidates = mechanism.retrieval.candidates || [];
  $("#retrievalCandidates").innerHTML = "<b>RETRIEVAL CANDIDATES</b>" + (candidates.length ? candidates.map(item => "<div class='rank-row " + (item.selected ? "selected" : "") + "'><b>" + esc(item.memoryId) + "</b><span>score " + item.score.toFixed(2) + " · " + esc(item.selected ? "selected" : item.rejectionReasons.join(", ")) + "</span><small>task=" + item.match.task + " checkpoint=" + item.match.checkpoint + "</small></div>").join("") : "<p>无候选记录</p>");
  $("#memories").innerHTML = memory.records.length ? memory.records.map(item => "<article><b>" + esc(item.preference) + "</b><small>trigger " + esc(item.trigger.task) + " / " + esc(item.trigger.step) + "</small><button data-delete='" + esc(item.id) + "'>删除</button></article>").join("") : "<span>暂无已确认偏好</span>";
  const episodic = state.episodicMemory || {recent: [], retrieval: {candidates: []}};
  const timelineViews = episodic.timelineViews || {};
  const visibleCards = [
    ...(timelineViews.participant?.recent || []),
    ...(timelineViews.case?.recent || []),
  ].filter((item, index, values) => values.findIndex(other => other.id === item.id) === index);
  const episodeCandidates = episodic.retrieval?.candidates || [];
  const episodeInjection = episodic.retrieval?.injection || {status: "not-injected", episodeIds: []};
  $("#episodicRetrieval").innerHTML = "<b>TIMELINE RETRIEVAL · " + esc(episodic.retrieval?.method || "none") + "</b><p>current-turn injection: " + esc(episodeInjection.status) + " · " + esc((episodeInjection.episodeIds || []).join(", ") || "none") + "</p>" + (episodeCandidates.length ? episodeCandidates.slice(0, 6).map(item => "<div class='rank-row " + (item.selected ? "selected" : "") + "'><b>" + esc(item.episodeId) + "</b><span>score " + Number(item.score || 0).toFixed(2) + " · " + esc(item.selected ? (item.retrievalReasons || ["selected"]).join(", ") : (item.rejectionReasons || []).join(", ")) + "</span><small>" + esc(item.record.kind + " / " + item.record.source) + "</small></div>").join("") : "<p>无时间线召回候选</p>");
  $("#episodicEpisodes").innerHTML = "<b>RECENT CARDS · raw media retained: " + esc(episodic.retainsRawMedia) + "</b>" + (visibleCards.length ? visibleCards.map(item => "<article><b>" + esc(item.text) + "</b><small>" + esc(item.kind + " · " + item.scope + " · " + item.tier) + "</small><button data-delete-card='" + esc(item.id) + "'>删除</button></article>").join("") : "<p>暂无活动时间线卡片</p>");
  $("#toolAudit").innerHTML = state.toolAudit.length ? state.toolAudit.map(item => "<article><b>" + esc(item.tool || item.action || "tool") + "</b><p>" + esc(JSON.stringify(item.arguments || item.result || {})) + "</p><small>" + esc(item.time || item.timestamp || "") + "</small></article>").join("") : "<span class='muted'>暂无工具调用</span>";
  $$("[data-delete]").forEach(button => button.onclick = () => runMemoryMutation(button, post("/api/memory/delete", {id: button.dataset.delete, sessionId: selectedSessionId})));
  $$("[data-delete-card]").forEach(button => button.onclick = () => runMemoryMutation(button, post("/api/memory/delete", {id: button.dataset.deleteCard, sessionId: selectedSessionId})));
  $("#memoryCandidate").innerHTML = memory.candidate ? "<div class='candidate'><b>要记住这个提示偏好吗？</b><p>" + esc(memory.candidate.preference) + "</p><button data-accept='true'>确认保存</button><button data-accept='false'>不要保存</button></div>" : "";
  $$("[data-accept]").forEach(button => button.onclick = () => runMemoryMutation(button, post("/api/candidate", {accepted: button.dataset.accept === "true", sessionId: selectedSessionId})));
  const scheduler = state.scheduler;
  $("#scheduler").innerHTML = "<b>Response scheduler</b><br>active: " + esc((scheduler.activeResponseIds || []).join(", ") || "none") + "<br>queued: " + esc(JSON.stringify(scheduler.queued)) + "<br>inflight: " + esc(JSON.stringify(scheduler.inflight)) + "<br>barge-in pending: " + esc(scheduler.bargeInPending) + "<br><b>Policy source</b>: " + esc(state.turnTakingPolicy.source) + "<br>selected: " + esc(JSON.stringify(state.turnTakingPolicy.currentSelection)) + "<br>protected max: " + esc(state.turnTakingPolicy.maxProtectionMs) + " ms";
  $("#intentRouting").textContent = JSON.stringify(state.intentRouting, null, 2);
  $("#audioAttribution").textContent = JSON.stringify(state.audioAttribution, null, 2);
  $$('[data-requires-task]').forEach(button => button.disabled = !task);
  $("#taskDebug").hidden = !task;
  renderWorkCenter();
  renderPipeline(task, mechanism, step);
  renderTimeline(state.events || []);
  renderManualSteps();
}

function defaultToolArguments(declaration) {
  return Object.fromEntries(Object.entries(declaration.arguments?.properties || {})
    .filter(([, field]) => Object.hasOwn(field, "default"))
    .map(([name, field]) => [name, field.default]));
}

async function executeTool(button) {
  const declaration = (state.activeSkill?.tools || []).find(item => item.name === button.dataset.tool);
  if (!declaration) return alert("当前 Skill 未声明该工具");
  const tool = declaration.name;
  const needsConfirmation = declaration.requires_confirmation !== false;
  const confirmed = !needsConfirmation || confirm("当前 Skill 请求调用 " + tool + "，是否允许？");
  if (!confirmed) return;
  const arguments = defaultToolArguments(declaration);
  try {
    const payload = await post("/api/tools/execute", {
      tool, arguments, confirmed, sessionId: selectedSessionId
    });
    $("#toolResult").textContent = JSON.stringify(payload.result);
    render(payload.state);
  } catch (error) { alert(error.message); }
}

function renderManualSteps() {
  if (!state) return;
  $("#manualSteps").innerHTML = state.task ? state.task.steps.map(item => "<button data-step='" + esc(item.id) + "'>外部观察：" + esc(item.title) + "</button>").join("") : "";
  $$("[data-step]").forEach(button => button.onclick = () => run(button, post("/api/task/observe", {
    step: button.dataset.step, completed: false, confidence: 0.92,
    summary: "外部感知：" + button.dataset.step, hazards: [], source: "manual/ui",
    sessionId: selectedSessionId
  })));
}

async function run(button, promise) {
  if (button) button.disabled = true;
  try {
    render(await promise);
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type: "refresh"}));
  } catch (error) { alert(error.message); }
  finally { if (button) button.disabled = false; }
}

async function runMemoryMutation(button, promise) {
  if (button) button.disabled = true;
  try {
    await promise;
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({type: "refresh"}));
    } else {
      render(await request(selectedStatePath()));
      const selected = sessionCatalog.find(item => item.id === selectedSessionId);
      if (selected?.memoryPolicy) renderMemoryPolicy(selected.memoryPolicy, state.memoryProfiles || []);
    }
  } catch (error) {
    alert(error.message);
  } finally {
    if (button) button.disabled = false;
  }
}

async function ensureMedia() {
  if (stream) return stream;
  if (mediaPromise) return mediaPromise;
  const generation = mediaGeneration;
  const attempt = (async () => {
    const acquired = await navigator.mediaDevices.getUserMedia({
      video: {facingMode: {ideal: "environment"}, width: {ideal: 1280}, frameRate: {ideal: 30}},
      audio: {channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true}
    });
    if (generation !== mediaGeneration) {
      acquired.getTracks().forEach(track => track.stop());
      throw Error("设备访问已取消");
    }
    stream = acquired;
    $("#video").srcObject = stream;
    applyCameraOrientation();
    $("#cameraEmpty").hidden = true;
    $("#cameraState").textContent = "摄像头与麦克风在线";
    $("#cameraButton").textContent = "关闭设备";
    return stream;
  })();
  mediaPromise = attempt;
  try { return await attempt; }
  finally { if (mediaPromise === attempt) mediaPromise = null; }
}

function stopMedia() {
  mediaGeneration += 1;
  if (videoTimer) clearInterval(videoTimer);
  videoTimer = null;
  if (processor) processor.disconnect();
  if (microphoneSource) microphoneSource.disconnect();
  processor = microphoneSource = null;
  if (stream) stream.getTracks().forEach(track => track.stop());
  stream = null;
  $("#video").srcObject = null;
  $("#cameraEmpty").hidden = false;
  $("#cameraState").textContent = "摄像头未启动";
  $("#cameraButton").textContent = "启用设备";
}

function floatToPcm16Base64(floatSamples, sourceRate) {
  const ratio = sourceRate / 16000;
  const length = Math.floor(floatSamples.length / ratio);
  const pcm = new Int16Array(length);
  for (let index = 0; index < length; index++) {
    const sourceIndex = Math.floor(index * ratio);
    const sample = Math.max(-1, Math.min(1, floatSamples[sourceIndex]));
    pcm[index] = sample < 0 ? sample * 32768 : sample * 32767;
  }
  const bytes = new Uint8Array(pcm.buffer);
  let binary = "";
  for (let offset = 0; offset < bytes.length; offset += 8192) binary += String.fromCharCode(...bytes.subarray(offset, offset + 8192));
  return btoa(binary);
}

async function startAudioCapture() {
  if (processor) return;
  audioContext = audioContext || new AudioContext();
  await audioContext.resume();
  microphoneSource = audioContext.createMediaStreamSource(stream);
  processor = audioContext.createScriptProcessor(4096, 1, 1);
  processor.onaudioprocess = event => {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    if (socket.bufferedAmount > AUDIO_BUFFER_HIGH_WATER) {
      droppedAudioChunks += 1;
      return;
    }
    if (droppedAudioChunks) {
      socket.send(JSON.stringify({
        type: "media_gap", media: "audio", droppedChunks: droppedAudioChunks,
        reason: "browser-websocket-backpressure",
      }));
      droppedAudioChunks = 0;
    }
    const audio = floatToPcm16Base64(event.inputBuffer.getChannelData(0), audioContext.sampleRate);
    socket.send(JSON.stringify({type: "audio", audio}));
  };
  microphoneSource.connect(processor);
  processor.connect(audioContext.destination);
}

function sendVideoFrame() {
  if (!socket || socket.readyState !== WebSocket.OPEN || !stream || $("#video").readyState < 2) return;
  if (socket.bufferedAmount > VIDEO_BUFFER_HIGH_WATER) {
    droppedVideoFrames += 1;
    return;
  }
  if (droppedVideoFrames) {
    socket.send(JSON.stringify({
      type: "media_gap", media: "video", droppedFrames: droppedVideoFrames,
      reason: "browser-websocket-backpressure",
    }));
    droppedVideoFrames = 0;
  }
  const video = $("#video"), canvas = $("#canvas"), scale = Math.min(1, 768 / video.videoWidth);
  canvas.width = Math.floor(video.videoWidth * scale);
  canvas.height = Math.floor(video.videoHeight * scale);
  const context = canvas.getContext("2d");
  context.save();
  if (mirrorCamera) {
    context.translate(canvas.width, 0);
    context.scale(-1, 1);
  }
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  context.restore();
  const image = canvas.toDataURL("image/jpeg", 0.72).split(",", 2)[1];
  socket.send(JSON.stringify({type: "video", image}));
}

function applyCameraOrientation() {
  $("#video").classList.toggle("mirrored", mirrorCamera);
}

function sendMediaConfiguration(ws) {
  const track = stream?.getAudioTracks()[0];
  const settings = track?.getSettings ? track.getSettings() : {};
  ws.send(JSON.stringify({
    type: "media_config",
    playbackCapture: false,
    clock: {
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
      utcOffsetMinutes: -new Date().getTimezoneOffset()
    },
    video: {
      mirrored: mirrorCamera,
      previewMatchesModelInput: true,
      coordinateConvention: mirrorCamera ? "horizontally-mirrored" : "camera-native"
    },
    audio: {
      deviceId: settings.deviceId ? "present" : "unreported",
      channelCount: settings.channelCount,
      sampleRate: settings.sampleRate,
      echoCancellation: settings.echoCancellation,
      noiseSuppression: settings.noiseSuppression,
      autoGainControl: settings.autoGainControl
    }
  }));
}

function playPcm24(base64Audio) {
  if (!audioContext) return;
  const binary = atob(base64Audio), pcm = new Int16Array(Math.floor(binary.length / 2));
  for (let index = 0; index < pcm.length; index++) pcm[index] = binary.charCodeAt(index * 2) | (binary.charCodeAt(index * 2 + 1) << 8);
  const buffer = audioContext.createBuffer(1, pcm.length, 24000);
  const channel = buffer.getChannelData(0);
  for (let index = 0; index < pcm.length; index++) channel[index] = pcm[index] / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.connect(audioContext.destination);
  const now = audioContext.currentTime;
  playbackTime = Math.max(playbackTime, now + 0.03);
  source.start(playbackTime);
  playbackSources.add(source);
  source.onended = () => playbackSources.delete(source);
  playbackTime += buffer.duration;
}

function stopPlayback() {
  playbackSources.forEach(source => { try { source.stop(); } catch (_) {} });
  playbackSources.clear();
  playbackTime = audioContext ? audioContext.currentTime : 0;
}

async function connectRealtime() {
  if (socket && socket.readyState === WebSocket.OPEN && !connectingPromise) return;
  if (connectingPromise) return connectingPromise;
  const generation = ++connectionGeneration;
  const attempt = connectRealtimeSession(generation);
  connectingPromise = attempt;
  try { await attempt; }
  finally { if (connectingPromise === attempt) connectingPromise = null; }
}

async function connectRealtimeSession(generation) {
  if (!selectedSessionId) await refreshSessionList();
  if (generation !== connectionGeneration) return;
  const connectionSessionId = selectedSessionId;
  await ensureMedia();
  if (generation !== connectionGeneration) return;
  await startAudioCapture();
  if (generation !== connectionGeneration) {
    stopAudioCapture();
    return;
  }
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(scheme + "//" + location.host + "/ws/realtime?session=" + encodeURIComponent(connectionSessionId));
  if (generation !== connectionGeneration) {
    ws.close();
    return;
  }
  socket = ws;
  let ready = false;
  let restartAfterClose = false;
  let resolveReady, rejectReady;
  const readyPromise = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  ws.onopen = () => {
    if (socket !== ws || generation !== connectionGeneration) { ws.close(); return; }
    $("#duplexState").textContent = "正在连接实时模型…";
    const fps = Number($("#videoFps").value);
    if (videoTimer) clearInterval(videoTimer);
    videoTimer = setInterval(sendVideoFrame, 1000 / fps);
  };
  ws.onmessage = event => {
    if (socket !== ws || generation !== connectionGeneration) return;
    const message = JSON.parse(event.data);
    if (message.type === "ready") {
      ready = true;
      selectedSessionId = message.contextSessionId || selectedSessionId;
      if (currentUser) localStorage.setItem("egoassist.session." + currentUser.id, selectedSessionId);
      refreshSessionList(selectedSessionId).catch(() => {});
      resolveReady();
      sendMediaConfiguration(ws);
      $("#duplexState").textContent = "全双工在线 · 直接说话，可随时打断";
      $("#realtimeButton").textContent = "结束陪练";
    } else if (message.type === "audio" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
      $("#duplexState").textContent = "助手正在说 · 你可以随时打断";
      playPcm24(message.audio);
    } else if (message.type === "interrupted") {
      stopPlayback();
      $("#duplexState").textContent = "已打断旧响应，正在处理你的请求";
    } else if (message.type === "barge_in_policy") {
      $("#duplexState").textContent = message.action === "protected" ? "安全句保护播报中 · 仍在听你说话" : "将在当前短句边界停止 · 仍在听你说话";
    } else if (message.type === "user_transcript") {
      $("#transcript").textContent = "你 · " + (message.channel || "input_audio_transcription") + "：" + message.text + (message.echoRisk === "suspected" ? " · 可能残余回声" : "");
      $("#duplexState").textContent = "正在听你说话";
    } else if (message.type === "assistant_transcript" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
      assistantText = message.done ? message.text : assistantText + message.text;
      $("#assistantCaption").innerHTML = "<span>助手语音字幕 · provider output channel · " + (message.done ? "已完成" : "生成中") + "</span><p>" + esc(assistantText) + "</p>";
      $("#duplexState").textContent = message.done ? "全双工在线 · 正在听" : "助手正在说 · 你可以随时打断";
      if (message.done) assistantText = "";
    } else if (message.type === "memory_status") {
      $("#assistantCaption").innerHTML = "<span>记忆事务 · 服务端确认</span><p>" + esc(message.message) + "</p>";
      $("#duplexState").textContent = message.status === "committed"
        ? "记忆已提交 · 正在听"
        : message.status === "needs-input" ? "等待补充要记住的内容" : "记忆未提交 · 正在听";
    } else if (message.type === "tool_confirmation_required") {
      const details = JSON.stringify(message.arguments || {}, null, 2);
      const accepted = window.confirm("Allow tool " + message.tool + "?\n\n" + details);
      if (socket === ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: "tool_confirmation", callId: message.callId, accepted}));
      }
    } else if (message.type === "media_gap") {
      $("#duplexState").textContent = "Media gap detected; live capture continues";
    } else if (message.type === "state") {
      render(message.state);
    } else if (message.type === "session_stale") {
      restartAfterClose = true;
      $("#duplexState").textContent = "任务已切换，正在重建实时会话…";
    } else if (message.type === "reconnecting") {
      $("#duplexState").textContent = "实时模型会话正在自动恢复 · 麦克风音频短暂缓冲中";
    } else if (message.type === "error") {
      alert(message.message);
    }
  };
  ws.onerror = () => { if (!ready && generation === connectionGeneration) rejectReady(Error("实时 WebSocket 连接失败")); };
  ws.onclose = () => {
    if (generation !== connectionGeneration) { resolveReady(); return; }
    if (!ready && generation === connectionGeneration) rejectReady(Error("实时模型连接在就绪前关闭"));
    if (socket !== ws) return;
    $("#duplexState").textContent = "实时会话已结束";
    $("#realtimeButton").textContent = "启动全双工陪练";
    socket = null;
    if (restartAfterClose && stream && generation === connectionGeneration) {
      setTimeout(() => connectRealtime().catch(error => alert("重连失败：" + error.message)), 0);
    }
  };
  return readyPromise;
}

function stopAudioCapture() {
  if (processor) processor.disconnect();
  if (microphoneSource) microphoneSource.disconnect();
  processor = microphoneSource = null;
}

function disconnectRealtime() {
  connectionGeneration += 1;
  if (mediaPromise) mediaGeneration += 1;
  stopPlayback();
  const ws = socket;
  socket = null;
  if (ws) ws.close();
  if (videoTimer) clearInterval(videoTimer);
  videoTimer = null;
  stopAudioCapture();
  $("#duplexState").textContent = "实时会话已结束";
  $("#realtimeButton").textContent = "启动全双工陪练";
}

async function sendControl(button) {
  button.disabled = true;
  try {
    await connectRealtime();
    if (!socket || socket.readyState !== WebSocket.OPEN) throw Error("实时会话尚未就绪");
    socket.send(JSON.stringify({type: "control", transcript: button.dataset.say}));
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
}

async function switchRuntime(button, path, payload) {
  const wasRealtime = Boolean(socket || connectingPromise);
  const pendingConnection = connectingPromise;
  if (wasRealtime) disconnectRealtime();
  if (pendingConnection) await pendingConnection.catch(() => {});
  button.disabled = true;
  try {
    render(await post(path, {...payload, sessionId: selectedSessionId}));
    if (wasRealtime) await connectRealtime();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
}

$("#realtimeButton").onclick = async () => { try { socket || connectingPromise ? disconnectRealtime() : await connectRealtime(); } catch (error) { alert("启动失败：" + error.message); disconnectRealtime(); } };
$("#cameraButton").onclick = async () => { try { stream || mediaPromise ? stopMedia() : await ensureMedia(); } catch (error) { alert("设备访问失败：" + error.message); } };
$("#videoFps").onchange = () => { if (!socket) return; clearInterval(videoTimer); videoTimer = setInterval(sendVideoFrame, 1000 / Number($("#videoFps").value)); };
$("#mirrorCamera").checked = mirrorCamera;
applyCameraOrientation();
$("#mirrorCamera").onchange = event => {
  mirrorCamera = event.currentTarget.checked;
  localStorage.setItem("egoassist.camera.mirror", String(mirrorCamera));
  applyCameraOrientation();
  if (socket?.readyState === WebSocket.OPEN) sendMediaConfiguration(socket);
};
$$("[data-say]").forEach(button => button.onclick = () => sendControl(button));
$("#reset").onclick = event => switchRuntime(event.currentTarget, "/api/task/reset", {});
$("#skillSelect").onchange = event => switchRuntime(event.currentTarget, "/api/skills/activate", {skill: event.currentTarget.value});
$("#taskSelect").onchange = event => { if (event.currentTarget.value) switchRuntime(event.currentTarget, "/api/tasks/select", {task: event.currentTarget.value}); };
$("#memoryProfile").onchange = event => {
  if (event.currentTarget.value !== "custom") saveMemoryPolicy({profile: event.currentTarget.value});
};
$("#preferenceEnabled").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$("#preferenceScope").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$("#episodicEnabled").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$("#episodicScope").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$$('[data-replay]').forEach(button => button.onclick = () => run(button, post("/api/replay/observe", {
  kind: button.dataset.replay, sessionId: selectedSessionId
})));
$("#newTask").onclick = () => $("#taskDialog").showModal();
$("#cancelTask").onclick = () => $("#taskDialog").close();
$("#taskDialog").onclose = () => $("#taskForm").reset();
$("#taskForm").onsubmit = async event => {
  event.preventDefault();
  const title = $("#customTitle").value.trim();
  const steps = $("#customSteps").value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  try {
    const wasRealtime = Boolean(socket || connectingPromise);
    const pendingConnection = connectingPromise;
    if (wasRealtime) disconnectRealtime();
    if (pendingConnection) await pendingConnection.catch(() => {});
    render(await post("/api/tasks/create", {title, steps, sessionId: selectedSessionId}));
    $("#taskDialog").close();
    if (wasRealtime) await connectRealtime();
  } catch (error) { alert(error.message); }
};
$("#reminderForm").onsubmit = async event => {
  event.preventDefault();
  const button = $("#createReminder");
  button.disabled = true;
  try {
    const payload = await post("/api/reminders", {
      sessionId: selectedSessionId,
      seconds: Number($("#reminderSeconds").value),
      label: $("#reminderLabel").value.trim(),
    });
    render(payload.state);
  } catch (error) { alert(error.message); }
  finally { if (state) renderWorkCenter(); }
};

$("#loginMode").onclick = () => setAuthMode("login");
$("#registerMode").onclick = () => setAuthMode("register");
$("#authForm").onsubmit = async event => {
  event.preventDefault();
  $("#authSubmit").disabled = true;
  $("#authError").textContent = "";
  try {
    const payload = await post("/api/auth/" + authMode, {
      username: $("#authUsername").value.trim(),
      password: $("#authPassword").value,
    });
    await enterConsole(payload.user);
  } catch (error) {
    $("#authError").textContent = error.message;
  } finally {
    $("#authSubmit").disabled = false;
  }
};
$("#logout").onclick = async () => {
  disconnectRealtime();
  stopMedia();
  try { await post("/api/auth/logout"); } finally { showLogin(); }
};
$("#newSession").onclick = async () => {
  const label = prompt("实验案例名称", "新实验案例");
  if (label === null || !label.trim()) return;
  const wasRealtime = Boolean(socket || connectingPromise);
  const pendingConnection = connectingPromise;
  disconnectRealtime();
  if (pendingConnection) await pendingConnection.catch(() => {});
  const created = await post("/api/sessions", {label: label.trim()});
  selectedSessionId = created.session.id;
  if (currentUser) localStorage.setItem("egoassist.session." + currentUser.id, selectedSessionId);
  await refreshSessionList(selectedSessionId);
  if (wasRealtime) await connectRealtime();
  else render(await request(selectedStatePath()));
};
$("#sessionSelect").onchange = async event => {
  const wasRealtime = Boolean(socket || connectingPromise);
  const pendingConnection = connectingPromise;
  disconnectRealtime();
  if (pendingConnection) await pendingConnection.catch(() => {});
  selectedSessionId = event.currentTarget.value;
  if (currentUser) localStorage.setItem("egoassist.session." + currentUser.id, selectedSessionId);
  const selected = sessionCatalog.find(item => item.id === selectedSessionId);
  if (selected?.memoryPolicy) renderMemoryPolicy(selected.memoryPolicy, state?.memoryProfiles || []);
  if (wasRealtime) await connectRealtime();
  else render(await request(selectedStatePath()));
};
boot().catch(error => { showLogin(); $("#authError").textContent = error.message; });
setInterval(() => {
  if (!socket || socket.readyState !== WebSocket.OPEN) return;
  socket.send(JSON.stringify({type: "refresh"}));
}, 1200);
addEventListener("beforeunload", () => { disconnectRealtime(); stopMedia(); });
