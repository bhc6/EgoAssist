const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
let state, stream, socket, audioContext, microphoneSource, processor, videoTimer;
let livekitRoom = null, livekitAudioElements = [];
let interactionTransport = "websocket-fallback";
let playbackTime = 0, assistantText = "";
let connectingPromise = null;
let connectionGeneration = 0;
let mediaPromise = null;
let mediaGeneration = 0;
let selectedEventId = null, pipelineDetails = [];
let currentUser = null, selectedSessionId = null, authMode = "login";
let sessionCatalog = [], memoryPolicySaving = false;
let ambientState = null;
let pdfjs = null, artifactDocument = null, artifactPageNumber = 1, artifactScale = 1;
let artifactRenderGeneration = 0, artifactRenderTask = null, pendingHumanHelpPreview = null;
let mirrorCamera = localStorage.getItem("egoassist.camera.mirror") !== "false";
const playbackSources = new Set();
const AUDIO_BUFFER_HIGH_WATER = 512 * 1024;
const VIDEO_BUFFER_HIGH_WATER = 128 * 1024;
let droppedAudioChunks = 0;
let droppedVideoFrames = 0;
const workflow = {turn: 0, active: false, stages: {}, activity: []};
const workflowStages = [
  ["input", "理解请求"],
  ["reasoning", "思考中"],
  ["memory", "记忆检索中"],
  ["action", "执行与校验"],
  ["delivery", "组织回答"],
];

function resetWorkflow(detail) {
  workflow.turn += 1;
  workflow.active = true;
  workflow.stages = {input: {status: "active", detail, at: Date.now()}};
  workflow.activity = [];
  recordWorkflow("input", detail);
}

function setWorkflowStage(stage, status, detail) {
  if (!workflow.active && status === "active") workflow.active = true;
  workflow.stages[stage] = {status, detail, at: Date.now()};
  if (detail) recordWorkflow(stage, detail);
  renderLiveWorkflow();
}

function recordWorkflow(stage, detail) {
  const previous = workflow.activity.at(-1);
  if (previous?.stage === stage && previous.detail === detail) return;
  workflow.activity.push({stage, detail, at: Date.now()});
  workflow.activity = workflow.activity.slice(-8);
}

function renderLiveWorkflow() {
  const root = $("#liveWorkflow");
  if (!root) return;
  const stages = workflowStages.map(([key, label]) => {
    const item = workflow.stages[key] || {status: "waiting", detail: "等待前序可观测事件"};
    const statusLabel = {active: "进行中", complete: "已完成", skipped: "未使用", failed: "失败", waiting: "等待"}[item.status] || item.status;
    return "<article class='workflow-stage " + esc(item.status) + "'><span class='workflow-marker'></span><div><b>" + esc(label) + "</b><small>" + esc(item.detail) + "</small></div><em>" + esc(statusLabel) + "</em></article>";
  }).join("");
  const activity = workflow.activity.length
    ? workflow.activity.slice().reverse().map(item => "<li><time>" + new Date(item.at).toLocaleTimeString("zh-CN", {hour12: false}) + "</time><b>" + esc(workflowStages.find(stage => stage[0] === item.stage)?.[1] || item.stage) + "</b><span>" + esc(item.detail) + "</span></li>").join("")
    : "<li class='workflow-empty'>等待真实运行事件</li>";
  root.innerHTML = "<div class='workflow-head'><b>当前工作流</b><span>仅展示事件、检索、工具和响应状态，不展示隐藏思维链</span></div><div class='workflow-stages'>" + stages + "</div><ol class='workflow-activity'>" + activity + "</ol>";
}

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
  const timeline = policy.components.timeline;
  $("#timelineEnabled").checked = timeline.enabled;
  $("#timelineScope").value = timeline.scope;
  $("#timelineScope").disabled = !timeline.enabled || memoryPolicySaving;
  $("#memoryProfile").disabled = memoryPolicySaving;
  $("#timelineEnabled").disabled = memoryPolicySaving;
  $("#memoryPolicyStatus").textContent = memoryPolicySaving
    ? "正在保存实验配置…"
    : "policy v" + policy.version + " · context journal 始终用于恢复与审计 · 变更从后续轮次生效";
}

function customMemoryPolicy() {
  const enabled = $("#timelineEnabled").checked;
  return {
    profile: "custom",
    components: {
      timeline: {
        enabled,
        preference_enabled: enabled,
        episodic_enabled: enabled,
        scope: $("#timelineScope").value,
      },
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
  await refreshPrivacy();
}

function renderPrivacy(payload) {
  if (!payload) return;
  if (payload.ambient) {
    ambientState = {...(ambientState || {}), ...payload.ambient};
    renderAmbient();
  }
  $("#privacyStatus").textContent = payload.transport?.transportStatus || "已加载";
  $("#privacyProviders").innerHTML = (payload.providers || []).map(item =>
    "<article class='privacy-card'><b>" + esc(item.provider) + "</b><span>数据：" + esc((item.dataCategories || []).join(", ")) + "</span><span>用途：" + esc((item.purposes || []).join(", ")) + "</span><span>保留：" + esc(item.retention) + "</span></article>"
  ).join("") || "<p>暂无供应商披露</p>";
  const evaluation = payload.evaluation || {};
  $("#privacyEvaluation").textContent = "真实结果样本 " + (evaluation.denominator || 0) + " · " + (evaluation.provenance || "等待用户确认、连接器回执或标注序列");
  const policies = payload.retentionPolicies || [];
  $("#privacyRetention").innerHTML = ["transcript", "observation", "operational", "memory"].map(category => {
    const item = policies.find(value => value.category === category);
    return "<label>" + esc(category) + "<input type='number' min='0' max='36500' data-retention-category='" + esc(category) + "' value='" + esc(item?.retention_days ?? 365) + "'></label>";
  }).join("") + "<button id='saveRetention' type='button'>保存保留期限</button>";
  $$('[data-retention-category]').forEach(input => input.onchange = () => { $("#privacyPolicyStatus").textContent = "待保存"; });
  $("#saveRetention").onclick = async () => {
    try {
      for (const input of $$('[data-retention-category]')) await post("/api/privacy/retention", {category: input.dataset.retentionCategory, retentionDays: Number(input.value)});
      await refreshPrivacy();
      $("#privacyPolicyStatus").textContent = "已保存";
    } catch (error) { alert(error.message); }
  };
}

async function refreshPrivacy() {
  try { renderPrivacy(await request("/api/privacy")); }
  catch (error) { if (currentUser) $("#privacyStatus").textContent = error.message; }
}

async function downloadPrivacyExport() {
  const response = await fetch("/api/privacy/export");
  if (!response.ok) throw Error("导出失败");
  const blob = await response.blob();
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob); link.download = "egoassist-account-export.json"; link.click();
  setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

async function boot() {
  const payload = await request("/api/auth/me");
  if (payload.user) await enterConsole(payload.user);
  else showLogin();
}

const actionName = action => ({silent: "静默", guide: "指导", ask: "询问", correct: "纠错", emergency: "紧急"}[action] || action);
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
    {stage: "perception", title: "结构化感知", status: task?.observation ? "live" : lanes.perception?.connected ? "warning" : "idle", summary: task?.observation ? task.observation.step + " · age " + task.observation.frameAgeMs + " ms" : lanes.perception?.connected ? "等待任务绑定的新鲜帧" : "感知通道未连接", claim: "每帧绑定 task/version/checkpoint/epoch 与允许路径；模型只产出证据，用户确认才推进 checkpoint"},
    {stage: "task", title: "Skill / 任务假设", status: task ? "live" : state.activeSkill ? "warning" : "idle", summary: taskSummary, claim: task ? "Checkpoint ledger，不规定唯一执行路径" : "无证据时不加载领域工作流"},
    {stage: "knowledge", title: "知识增强", status: state.knowledgeMechanism.method === "none" ? "idle" : "warning", summary: state.knowledgeMechanism.method, claim: state.knowledgeMechanism.claimBoundary},
    {stage: "memory", title: "TimelineMemory 检索", status: retrieval.status === "matched" || state.episodicMemory?.count ? "live" : "idle", summary: "timeline " + (state.episodicMemory?.count || 0) + " cards · 注入 " + (state.episodicMemory?.retrieval?.injection?.evidence?.length || 0) + " · " + (state.episodicMemory?.retrieval?.method || "hybrid"), claim: "scene/event/fact/state/observation 全类型统一检索；BM25 + 语义双路；北京时间标注注入证据"},
    {stage: "policy", title: "主动干预策略", status: "live", summary: actionName(decision.action) + " · " + Math.round((decision.confidence || 0) * 100) + "%", claim: decision.reason},
    {stage: "scheduler", title: "响应调度与打断", status: scheduler.pending || scheduler.activeResponseIds?.length ? "live" : "idle", summary: (lanes.dialogue?.turnDetection || "unknown") + " · " + (scheduler.activePolicies?.[0]?.speechPolicy || state.turnTakingPolicy.currentSelection.policy), claim: "转写后先检索与更新上下文，再显式创建唯一回答；Skill 可配置：" + state.turnTakingPolicy.currentSelection.basis},
    {stage: "output", title: "语音与字幕输出", status: scheduler.activeResponseIds?.length ? "live" : "idle", summary: scheduler.activeResponseIds?.join(", ") || "等待输出", claim: "只展示可验证输出和结构化中间产物，不声称访问隐藏思维链"}
  ];
  // liveWorkflow is the only visible workflow renderer. Keep pipelineDetails
  // as the inspector source so old deep links and tests remain compatible.
  const legacyPipeline = $("#pipeline");
  if (legacyPipeline) legacyPipeline.innerHTML = "";
  renderLiveWorkflow();
}

function syncWorkflowFromState(task, mechanism) {
  if (!workflow.active) return;
  const retrieval = mechanism?.retrieval || {};
  const candidates = retrieval.candidates || [];
  if (retrieval.status === "matched") {
    const selected = candidates.filter(item => item.selected).length;
    setWorkflowStage("memory", "complete", "检索完成：候选 " + candidates.length + "，选中 " + selected);
  } else if (["no-match", "none", "disabled"].includes(retrieval.status)) {
    setWorkflowStage("memory", "skipped", retrieval.status === "disabled" ? "当前记忆策略已关闭" : "本轮没有可注入的匹配记忆");
  }
  const decision = task?.decision || state.genericDecision;
  if (decision?.action) {
    setWorkflowStage("reasoning", "complete", "已形成可观测决策：" + actionName(decision.action) + " · 置信度 " + Math.round((decision.confidence || 0) * 100) + "%");
    if (!state.scheduler?.pending && !(state.scheduler?.activeResponseIds || []).length) {
      setWorkflowStage("action", "skipped", "当前决策不需要外部工具或后台任务");
      setWorkflowStage("delivery", "active", "决策已进入响应调度器");
    }
  }
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

async function renderArtifactPage() {
  if (!artifactDocument) return;
  const generation = ++artifactRenderGeneration;
  const previousTask = artifactRenderTask;
  if (previousTask) {
    previousTask.cancel();
    try { await previousTask.promise; }
    catch (error) { if (error?.name !== "RenderingCancelledException") throw error; }
  }
  if (generation !== artifactRenderGeneration) return;
  const page = await artifactDocument.getPage(artifactPageNumber);
  if (generation !== artifactRenderGeneration) return;
  const viewport = page.getViewport({scale: artifactScale});
  const ratio = Math.max(1, window.devicePixelRatio || 1);
  const canvas = $("#artifactCanvas");
  canvas.width = Math.floor(viewport.width * ratio);
  canvas.height = Math.floor(viewport.height * ratio);
  canvas.style.width = Math.floor(viewport.width) + "px";
  canvas.style.height = Math.floor(viewport.height) + "px";
  const renderTask = page.render({canvasContext: canvas.getContext("2d"), viewport, transform: [ratio, 0, 0, ratio, 0, 0]});
  artifactRenderTask = renderTask;
  try {
    await renderTask.promise;
  } catch (error) {
    if (error?.name !== "RenderingCancelledException") throw error;
    return;
  } finally {
    if (artifactRenderTask === renderTask) artifactRenderTask = null;
  }
  if (generation !== artifactRenderGeneration) return;
  $("#artifactPage").textContent = artifactPageNumber + " / " + artifactDocument.numPages;
  $("#artifactZoom").textContent = Math.round(artifactScale * 100) + "%";
  $("#artifactPrevious").disabled = artifactPageNumber <= 1;
  $("#artifactNext").disabled = artifactPageNumber >= artifactDocument.numPages;
}

async function openArtifact(artifactId) {
  const artifact = (state.artifacts || []).find(item => item.artifact_id === artifactId);
  if (!artifact) return alert("该资料不再属于当前任务步骤，请刷新后重试");
  artifactRenderGeneration += 1;
  artifactDocument = null;
  artifactPageNumber = 1;
  artifactScale = 1;
  $("#artifactTitle").textContent = artifact.title;
  $("#artifactProvenance").textContent = [
    artifact.provenance?.source, artifact.provenance?.skill,
    state.task?.task?.id, artifact.step_id, artifact.checksum,
  ].filter(Boolean).join(" · ");
  $("#artifactCanvas").hidden = artifact.media_type !== "application/pdf";
  $("#artifactImage").hidden = !artifact.media_type.startsWith("image/");
  $("#artifactDialog").showModal();
  const contentUrl = artifact.previewUrl + "?session=" + encodeURIComponent(selectedSessionId);
  try {
    if (artifact.media_type === "application/pdf") {
      pdfjs ||= await import("./vendor/pdf.mjs?v=6.2.108");
      pdfjs.GlobalWorkerOptions.workerSrc = "./vendor/pdf.worker.mjs?v=6.2.108";
      const loadedDocument = await pdfjs.getDocument({url: contentUrl, withCredentials: true}).promise;
      if (!$("#artifactDialog").open) {
        await loadedDocument.destroy();
        return;
      }
      artifactDocument = loadedDocument;
      await renderArtifactPage();
    } else {
      $("#artifactImage").src = contentUrl;
      $("#artifactPage").textContent = "image";
      $("#artifactZoom").textContent = "fit";
    }
  } catch (error) {
    $("#artifactDialog").close();
    alert("参考资料加载失败：" + error.message);
  }
}

function resetHumanHelpDialog() {
  pendingHumanHelpPreview = null;
  $("#humanHelpCompose").hidden = false;
  $("#humanHelpPreview").hidden = true;
  $("#humanHelpConsentRow").hidden = true;
  $("#humanHelpConsent").checked = false;
  $("#humanHelpBack").hidden = true;
  $("#humanHelpContinue").textContent = "检查发送内容";
}

function showHumanHelpPreview(payload) {
  pendingHumanHelpPreview = payload;
  const preview = payload.preview;
  $("#humanHelpCompose").hidden = true;
  $("#humanHelpPreview").hidden = false;
  $("#humanHelpPreview").innerHTML =
    "<dl><dt>用途</dt><dd>" + esc(preview.purpose) + "</dd>" +
    "<dt>正文</dt><dd>" + esc(preview.message) + "</dd>" +
    "<dt>任务上下文</dt><dd>" + esc(Object.values(preview.taskContext).filter(Boolean).join(" / ") || "未加载任务") + "</dd>" +
    "<dt>数据类别</dt><dd>" + esc(preview.dataCategories.join("、")) + "</dd>" +
    "<dt>接收方</dt><dd>" + esc(preview.destination) + "</dd>" +
    "<dt>授权有效期</dt><dd>" + esc(payload.expiresAt) + "</dd></dl>";
  $("#humanHelpConsentRow").hidden = false;
  $("#humanHelpBack").hidden = false;
  $("#humanHelpContinue").textContent = "同意并发送";
}

function renderWorkCenter() {
  const ledger = state.workLedger || {intents: [], jobs: [], resultInbox: [], foregroundIntentId: null};
  const capabilities = state.capabilityCatalog?.platform || [];
  const unavailable = capabilities.filter(item => item.status !== "available");
  const timerCapability = capabilities.find(item => item.id === "timer-delivery") || {
    status: "unsupported", reason: "Temporal reminder service is not configured.",
  };
  const humanHelpCapability = capabilities.find(item => item.id === "human-help") || {
    status: "unconfigured", reason: "No human-help connector is configured.",
  };
  $("#createReminder").disabled = timerCapability.status !== "available";
  $("#reminderAvailability").textContent = timerCapability.status === "available"
    ? "Temporal · durable"
    : timerCapability.status + " · " + timerCapability.reason;
  $("#requestHumanHelp").disabled = humanHelpCapability.status !== "available";
  $("#humanHelpAvailability").textContent = humanHelpCapability.status === "available"
    ? "webhook · consent required" : humanHelpCapability.status + " · " + humanHelpCapability.reason;
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
    const artifact = item.content?.artifact || item.content?.result?.artifact;
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(content) +
      "</b><small>" + esc(item.status + " · priority " + item.priority) + "</small></div>" +
      "<div class='work-row-actions'>" +
      (artifact ? "<button data-open-artifact='" + esc(artifact.artifact_id) + "'>查看</button>" : "") +
      (dismissible ? "<button data-dismiss-result='" + esc(item.resultId) + "'>确认</button>" : "") + "</div>" +
      "</article>";
  }).join("") : "<p class='empty-state'>暂无待处理结果</p>";

  $("#artifactList").innerHTML = (state.artifacts || []).length ? state.artifacts.map(item =>
    "<article class='work-row'><div><b>" + esc(item.title) + "</b><small>" +
    esc(item.media_type + " · " + item.step_id) + "</small></div><button data-open-artifact='" +
    esc(item.artifact_id) + "'>查看</button></article>"
  ).join("") : "<p class='empty-state compact'>当前步骤没有已授权资料</p>";

  $$('[data-focus-intent]').forEach(button => button.onclick = () => run(button, post("/api/intents/focus", {
    sessionId: selectedSessionId, intentId: button.dataset.focusIntent,
  }).then(payload => payload.state || payload)));
  $$('[data-cancel-job]').forEach(button => button.onclick = () => run(button, post("/api/jobs/cancel", {
    sessionId: selectedSessionId, jobId: button.dataset.cancelJob,
  }).then(payload => payload.state || payload)));
  $$('[data-dismiss-result]').forEach(button => button.onclick = () => run(button, post("/api/results/dismiss", {
    sessionId: selectedSessionId, resultId: button.dataset.dismissResult,
  }).then(payload => payload.state || payload)));
  $$('[data-open-artifact]').forEach(button => button.onclick = () => openArtifact(button.dataset.openArtifact));
}

// ── TimelineMemory helpers ───────────────────────────────────────────────────

const KIND_LABELS = {
  "scene": "场景", "event": "事件", "fact": "事实", "state": "状态",
  "preference": "偏好", "task-preference": "任务偏好", "observation": "观测",
  "tool-effect": "工具效果", "legacy": "旧格式",
};
const KIND_CLASS = {
  "scene": "kind-scene", "event": "kind-event", "fact": "kind-fact",
  "state": "kind-state", "preference": "kind-pref", "observation": "kind-obs",
  "tool-effect": "kind-tool",
};
const kindLabel = kind => KIND_LABELS[kind] || kind || "?";
const kindClass = kind => KIND_CLASS[kind] || "kind-other";

function fmtBeijing(isoStr) {
  if (!isoStr) return "";
  try {
    const d = new Date(isoStr);
    return d.toLocaleString("zh-CN", {
      timeZone: "Asia/Shanghai", hour12: false,
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
    }).replace(/\//g, "-") + " 北京";
  } catch (_) { return isoStr; }
}

function renderTimelineCards(episodic) {
  const timelineViews = episodic.timelineViews || {};
  const allCards = [
    ...(timelineViews.participant?.recent || []),
    ...(timelineViews.case?.recent || []),
    ...(episodic.recent || []),
  ].filter((c, i, arr) => arr.findIndex(x => x.id === c.id) === i)
   .sort((a, b) => ((b.happenedAt || b.recordedAt || "") > (a.happenedAt || a.recordedAt || "") ? 1 : -1));

  const candidates = episodic.retrieval?.candidates || [];
  const injection = episodic.retrieval?.injection || {status: "not-injected", episodeIds: []};
  const selectedIds = new Set((injection.evidence || []).map(ev => ev.episodeId || ev.cardId));

  // Retrieval summary panel
  const summaryEl = $("#timelineRetrievalSummary");
  if (summaryEl) {
    const method = episodic.retrieval?.method || "none";
    const scope = episodic.retrieval?.scope || {};
    const injEvidence = injection.evidence || [];
    summaryEl.innerHTML =
      "<header class='tl-retrieval-header'><b>当前轮次 TimelineMemory 检索</b>" +
      " <code>" + esc(method) + "</code>" +
      " · 范围 <b>" + esc(scope.memoryScope || scope.session || "—") + "</b></header>" +
      "<div class='tl-retrieval-stats'>" +
        "<span>候选 <b>" + candidates.length + "</b></span>" +
        "<span>注入 <b>" + injEvidence.length + "</b></span>" +
        // current-turn injection: retrieval feeds THIS turn's context only; prior
        // turns are never re-generated (context injection, not regeneration).
        "<span class='tl-inject-status tl-status-" + esc(injection.status.replace(/[^a-z-]/g, "")) + "' title='current-turn injection'>" + esc(injection.status) + "</span>" +
      "</div>" +
      (injEvidence.length ? "<ul class='tl-evidence-list'>" + injEvidence.map(ev =>
        "<li><span class='kind-badge " + kindClass(ev.kind) + "'>" + esc(kindLabel(ev.kind)) + "</span>" +
        "<time class='tl-time'>" + esc(fmtBeijing(ev.timestamp)) + "</time>" +
        "<span class='tl-ev-text'>" + esc((ev.summary || "").slice(0, 100)) + "</span>" +
        "<small class='tl-ev-meta'>score " + Number(ev.score || 0).toFixed(2) + " · " + esc(ev.source || "") + "</small></li>"
      ).join("") + "</ul>" : "<p class='tl-no-evidence'>本轮无注入证据</p>") +
      (candidates.length > 0 ? "<details class='tl-candidates-detail'><summary>全部候选 (" + candidates.length + ")</summary><ul class='tl-candidates'>" +
        candidates.slice(0, 12).map(c =>
          "<li class='" + (c.selected ? "tl-selected" : "") + "'>" +
          "<span class='kind-badge " + kindClass(c.record?.kind) + "'>" + esc(kindLabel(c.record?.kind)) + "</span>" +
          "<span class='tl-cand-text'>" + esc((c.record?.summary || "").slice(0, 80)) + "</span>" +
          "<small>score " + Number(c.score || 0).toFixed(2) +
            (c.selected ? " · 已注入" : " · " + esc((c.rejectionReasons || []).join("/"))) + "</small></li>"
        ).join("") + "</ul></details>" : "");
  }

  // Cards panel
  const cardsEl = $("#timelineCards");
  if (!cardsEl) return;
  if (!allCards.length) {
    cardsEl.innerHTML = "<p class='empty-state'>TimelineMemory 暂无活动卡片</p>";
    return;
  }
  cardsEl.innerHTML =
    "<div class='tl-cards-header'><b>RECENT CARDS</b><span class='tl-card-count'>" + allCards.length + " 条</span></div>" +
    allCards.slice(0, 24).map(card => {
      const isInjected = selectedIds.has(card.id);
      const ts = card.happenedAt || card.recordedAt || card.timestamp;
      return "<article class='tl-card" + (isInjected ? " tl-card--injected" : "") + "'>" +
        "<div class='tl-card-top'>" +
          "<span class='kind-badge " + kindClass(card.kind) + "'>" + esc(kindLabel(card.kind)) + "</span>" +
          (ts ? "<time class='tl-card-time'>" + esc(fmtBeijing(ts)) + "</time>" : "") +
          (isInjected ? "<span class='tl-injected-badge'>已注入</span>" : "") +
        "</div>" +
        "<p class='tl-card-text'>" + esc((card.text || card.summary || "").slice(0, 140)) + "</p>" +
        "<div class='tl-card-meta'>" +
          "<small>" + esc(card.scope || "case") + " · " + esc(card.tier || "short") + "</small>" +
          "<button data-delete-card='" + esc(card.id) + "' class='tl-delete'>删除</button>" +
        "</div>" +
        "</article>";
    }).join("");
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
  const latestRejection = task?.perception?.audit?.[0];
  $("#vision").textContent = task?.observation
    ? task.observation.step + " · accepted · age " + task.observation.frameAgeMs + " ms · " + Math.round(task.observation.confidence * 100) + "%"
    : latestRejection ? "rejected · " + latestRejection.reason : "等待任务绑定的结构化视觉证据";
  $("#memoryScope").textContent = "exact-trigger-lookup · " + (mechanism.scope.skill || "general") + " / " + (mechanism.scope.task || "unscoped");
  renderMemoryPolicy(state.memoryPolicy, state.memoryProfiles || []);
  const episodic = state.episodicMemory || {recent: [], retrieval: {candidates: []}};
  renderTimelineCards(episodic);
  $("#toolAudit").innerHTML = state.toolAudit.length ? state.toolAudit.map(item => "<article><b>" + esc(item.tool || item.action || "tool") + "</b><p>" + esc(JSON.stringify(item.arguments || item.result || {})) + "</p><small>" + esc(item.time || item.timestamp || "") + "</small></article>").join("") : "<span class='muted'>暂无工具调用</span>";
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
  syncWorkflowFromState(task, mechanism);
  renderTimeline(state.events || []);
  renderPerceptionEvidence(task);
  renderAmbient();
  renderManualSteps();
}

// Ambient observation must be visible for as long as it is running. ADR 005/007:
// a camera that watches without a task the user started is only acceptable if the
// user can see, at a glance, that it is on and what it is watching for.
function renderAmbient() {
  const root = $("#ambientBanner");
  if (!root) return;
  const info = ambientState;
  if (!info || !info.enabled) {
    root.hidden = true;
    return;
  }
  root.hidden = false;
  if (!info.consented) {
    root.className = "ambient-banner idle";
    root.innerHTML = "<b>被动观测未授权</b><span>已启用但未获得同意，不会发送任何画面。</span>" +
      "<button id='ambientGrant'>授权被动观测</button>";
    $("#ambientGrant").onclick = () => setAmbientConsent(true);
    return;
  }
  const watchlist = info.watchlist || {};
  const activities = (watchlist.activities || []).map(item => item.label || item.id).join("、");
  const places = (watchlist.locations || []).map(item => item.label || item.id).join("、");
  const latest = info.observation;
  root.className = "ambient-banner live";
  root.innerHTML = "<b>被动观测已授权</b>" +
    "<span>观察：" + esc(activities || "-") + " · 地点识别候选：" + esc(places || "-") +
    " · 每 " + esc(String(info.intervalSeconds || "?")) + " 秒一帧</span>" +
    "<span>" + (latest
      ? esc((latest.observed ? "已看到 " + (latest.activityId || "") : "本帧未看到该行为") +
          " · " + (latest.locationId || "unknown"))
      : "尚无观测结果") + "</span>" +
    "<span class='ambient-note'>不判断是否吞下、不判断药物种类；原始画面不留存。</span>" +
    "<button id='ambientRevoke'>撤回授权</button>";
  $("#ambientRevoke").onclick = () => setAmbientConsent(false);
}

async function setAmbientConsent(granted) {
  const response = await fetch("/api/ambient/consent", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({granted}),
  });
  const payload = await response.json();
  if (payload.error) {
    alert(payload.error);
    return;
  }
  ambientState = {...(ambientState || {}), ...payload.ambient};
  renderAmbient();
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
    if (payload.result?.artifact?.artifact_id) await openArtifact(payload.result.artifact.artifact_id);
  } catch (error) { alert(error.message); }
}

function renderPerceptionEvidence(task) {
  const root = $("#perceptionEvidence");
  if (!task) {
    root.innerHTML = "<p class='empty-state compact'>无活动任务感知上下文</p>";
    return;
  }
  const policy = task.perception || {};
  const observation = task.observation || null;
  const consistency = observation?.temporalConsistency || {count: 0, required: 2, consistent: false};
  const audit = policy.audit || [];
  const evidence = observation?.visibleEvidence || [];
  root.innerHTML = "<header><b>PERCEPTION EVIDENCE</b><span>" + esc(observation ? "accepted" : audit.length ? "rejected" : "waiting") + "</span></header>" +
    "<div class='perception-grid'>" +
      "<div><small>BOUND TASK / VERSION</small><b>" + esc(task.task.id + " / " + task.task.version) + "</b></div>" +
      "<div><small>CHECKPOINT / EPOCH</small><b>" + esc(policy.checkpointId + " / " + policy.checkpointEpoch) + "</b></div>" +
      "<div><small>ALLOWED PATH</small><b>" + esc((policy.allowedTransitions || []).join(" · ") || "none") + "</b></div>" +
      "<div><small>FRAME / AGE</small><b>" + esc(observation ? (observation.frameId || "manual") + " / " + observation.frameAgeMs + " ms" : "waiting") + "</b></div>" +
      "<div><small>CONSISTENCY</small><b>" + esc(consistency.count + " / " + consistency.required + (consistency.consistent ? " · consistent" : " · confirmation pending")) + "</b></div>" +
      "<div><small>CONTEXT ID</small><b>" + esc(observation?.contextId || policy.contextId || "-") + "</b></div>" +
    "</div>" +
    (evidence.length ? "<ul>" + evidence.map(item => "<li>" + esc(item) + "</li>").join("") + "</ul>" : "") +
    (audit.length ? "<div class='perception-audit'><small>LATEST REJECTION</small>" + esc(audit[0].reason + " · " + (audit[0].frameId || "no frame")) + "</div>" : "");
}

function renderManualSteps() {
  if (!state) return;
  const allowed = new Set(state.task ? [state.task.currentCheckpointId, ...(state.task.perception?.allowedTransitions || [])] : []);
  $("#manualSteps").innerHTML = state.task ? state.task.steps.map(item => "<button data-step='" + esc(item.id) + "' " + (allowed.has(item.id) ? "" : "disabled title='不在当前允许路径'") + ">研究证据：" + esc(item.title) + "</button>").join("") : "";
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
  disconnectLiveKit();
  $("#video").srcObject = null;
  $("#cameraEmpty").hidden = false;
  $("#cameraState").textContent = "摄像头未启动";
  $("#cameraButton").textContent = "启用设备";
}

function clearLiveKitAudio() {
  livekitAudioElements.forEach(element => element.remove());
  livekitAudioElements = [];
}

async function disconnectLiveKit() {
  const room = livekitRoom;
  livekitRoom = null;
  interactionTransport = "websocket-fallback";
  clearLiveKitAudio();
  if (room) await room.disconnect().catch(() => {});
}

async function activateWebSocketMediaFallback() {
  interactionTransport = "websocket-fallback";
  if (!stream || !socket || socket.readyState !== WebSocket.OPEN) return;
  await startAudioCapture();
  const fps = Number($("#videoFps").value);
  if (videoTimer) clearInterval(videoTimer);
  videoTimer = setInterval(sendVideoFrame, 1000 / fps);
}

async function connectLiveKitMedia(connectionSessionId) {
  if (!window.LivekitClient) return false;
  let credentials;
  try {
    credentials = await post("/api/livekit/token", {sessionId: connectionSessionId});
  } catch (_) {
    return false;
  }
  if (!credentials.enabled) return false;
  const {Room, RoomEvent, Track} = window.LivekitClient;
  const room = new Room({adaptiveStream: true, dynacast: true, disconnectOnPageLeave: true});
  room.on(RoomEvent.TrackSubscribed, track => {
    if (track.kind !== Track.Kind.Audio) return;
    const element = track.attach();
    element.autoplay = true;
    element.dataset.egoassistLivekitAudio = "true";
    document.body.appendChild(element);
    livekitAudioElements.push(element);
  });
  room.on(RoomEvent.TrackUnsubscribed, track => track.detach().forEach(element => element.remove()));
  room.on(RoomEvent.Reconnecting, () => {
    $("#duplexState").textContent = "LiveKit 媒体正在重连，控制通道保持在线";
  });
  room.on(RoomEvent.Reconnected, () => {
    interactionTransport = "livekit";
    stopAudioCapture();
    if (videoTimer) clearInterval(videoTimer);
    videoTimer = null;
    $("#duplexState").textContent = "LiveKit 媒体已恢复";
  });
  room.on(RoomEvent.Disconnected, () => {
    if (livekitRoom !== room) return;
    livekitRoom = null;
    clearLiveKitAudio();
    activateWebSocketMediaFallback().catch(() => {});
  });
  try {
    await room.connect(credentials.url, credentials.token, {autoSubscribe: true});
    const audioTrack = stream?.getAudioTracks()[0];
    const videoTrack = stream?.getVideoTracks()[0];
    if (audioTrack) {
      await room.localParticipant.publishTrack(audioTrack, {source: Track.Source.Microphone});
    }
    if (videoTrack) {
      await room.localParticipant.publishTrack(videoTrack, {source: Track.Source.Camera});
    }
  } catch (_) {
    await room.disconnect().catch(() => {});
    return false;
  }
  livekitRoom = room;
  interactionTransport = "livekit";
  return true;
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
  if (interactionTransport === "livekit") return;
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
  const livekitConnected = await connectLiveKitMedia(connectionSessionId);
  if (!livekitConnected) await startAudioCapture();
  if (generation !== connectionGeneration) {
    await disconnectLiveKit();
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
    if (interactionTransport !== "livekit") {
      startAudioCapture().catch(() => {});
      const fps = Number($("#videoFps").value);
      if (videoTimer) clearInterval(videoTimer);
      videoTimer = setInterval(sendVideoFrame, 1000 / fps);
    }
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
      interactionTransport = message.interactionTransport === "livekit" && livekitRoom
        ? "livekit" : interactionTransport;
      $("#duplexState").textContent = (interactionTransport === "livekit" ? "LiveKit" : "WebSocket fallback") + " · 直接说话，可随时打断";
      $("#realtimeButton").textContent = "结束陪练";
    } else if (message.type === "provider_unavailable") {
      $("#duplexState").textContent = "Provider offline · local console remains available";
      $("#assistantCaption").innerHTML = "<span>REALTIME PROVIDER UNAVAILABLE</span><p>" + esc(message.reason || "network or DNS unavailable") + "</p>";
    } else if (message.type === "audio" && interactionTransport !== "livekit" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
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
      resetWorkflow("收到用户转写：" + String(message.text || "").slice(0, 80));
      setWorkflowStage("input", "complete", "用户转写已归因并进入当前轮次");
      setWorkflowStage("reasoning", "active", "正在进行意图路由与上下文编排");
      setWorkflowStage("memory", "active", "正在查询当前范围的 TimelineMemory");
    } else if (message.type === "assistant_transcript" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
      assistantText = message.done ? message.text : assistantText + message.text;
      $("#assistantCaption").innerHTML = "<span>助手语音字幕 · provider output channel · " + (message.done ? "已完成" : "生成中") + "</span><p>" + esc(assistantText) + "</p>";
      $("#duplexState").textContent = message.done ? "全双工在线 · 正在听" : "助手正在说 · 你可以随时打断";
      setWorkflowStage("delivery", message.done ? "complete" : "active", message.done ? "回答已通过 provider 输出通道交付" : "正在生成并流式交付回答");
      if (message.done) workflow.active = false;
      if (message.done) assistantText = "";
    } else if (message.type === "memory_status") {
      $("#assistantCaption").innerHTML = "<span>记忆事务 · 服务端确认</span><p>" + esc(message.message) + "</p>";
      $("#duplexState").textContent = message.status === "committed"
        ? "记忆已提交 · 正在听"
        : message.status === "needs-input" ? "等待补充要记住的内容" : "记忆未提交 · 正在听";
    } else if (message.type === "memory_trace") {
      if (message.op === "retrieve") {
        setWorkflowStage("memory", "complete", "检索完成：候选 " + (message.candidate_count || 0) + "，选中 " + (message.selected_count || 0));
        setWorkflowStage("reasoning", "active", "正在基于检索证据与当前状态形成可交付决策");
      } else if (message.op === "working_memory_push") {
        setWorkflowStage("memory", "complete", "检索证据已注入当前工作上下文");
      }
    } else if (message.type === "task_update") {
      setWorkflowStage("action", ["done", "completed"].includes(message.status) ? "complete" : message.status === "failed" ? "failed" : "active", "后台任务 " + (message.kind || "job") + " · " + (message.status || "pending"));
    } else if (message.type === "tool_confirmation_required") {
      setWorkflowStage("action", "active", "工具 " + message.tool + " 等待用户确认");
      const details = JSON.stringify(message.arguments || {}, null, 2);
      const accepted = window.confirm("Allow tool " + message.tool + "?\n\n" + details);
      if (socket === ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({type: "tool_confirmation", callId: message.callId, accepted}));
      }
    } else if (message.type === "media_gap") {
      $("#duplexState").textContent = "Media gap detected; live capture continues";
    } else if (message.type === "state") {
      render(message.state);
    } else if (message.type === "ambient") {
      ambientState = {...(ambientState || {}), ...message.ambient, observation: message.ambient?.observation};
      renderAmbient();
    } else if (message.type === "session_stale") {
      restartAfterClose = true;
      $("#duplexState").textContent = "任务已切换，正在重建实时会话…";
    } else if (message.type === "reconnecting") {
      $("#duplexState").textContent = "实时模型会话正在自动恢复 · 麦克风音频短暂缓冲中";
    } else if (message.type === "transport_status") {
      $("#duplexState").textContent = message.status === "connected"
        ? "LiveKit 媒体在线" : "LiveKit 媒体已断开，正在切换回退通道";
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
  disconnectLiveKit();
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
$("#timelineEnabled").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$("#timelineScope").onchange = () => saveMemoryPolicy(customMemoryPolicy());
$("#privacyExport").onclick = async () => { try { await downloadPrivacyExport(); } catch (error) { alert(error.message); } };
$("#privacyDelete").onclick = async () => {
  if (!confirm("删除账户数据后不可恢复。继续？")) return;
  try {
    await post("/api/privacy/delete", {confirm: true});
    showLogin();
  } catch (error) { alert(error.message); }
};
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
$("#artifactClose").onclick = () => $("#artifactDialog").close();
$("#artifactPrevious").onclick = async () => {
  if (!artifactDocument || artifactPageNumber <= 1) return;
  artifactPageNumber -= 1;
  await renderArtifactPage();
};
$("#artifactNext").onclick = async () => {
  if (!artifactDocument || artifactPageNumber >= artifactDocument.numPages) return;
  artifactPageNumber += 1;
  await renderArtifactPage();
};
$("#artifactZoomOut").onclick = async () => {
  if (!artifactDocument) return;
  artifactScale = Math.max(0.5, artifactScale - 0.15);
  await renderArtifactPage();
};
$("#artifactZoomIn").onclick = async () => {
  if (!artifactDocument) return;
  artifactScale = Math.min(2.5, artifactScale + 0.15);
  await renderArtifactPage();
};
$("#artifactDialog").onclose = () => {
  artifactRenderGeneration += 1;
  artifactRenderTask?.cancel();
  artifactRenderTask = null;
  if (typeof artifactDocument?.destroy === "function") {
    Promise.resolve(artifactDocument.destroy()).catch(() => {});
  }
  artifactDocument = null;
  $("#artifactImage").removeAttribute("src");
};
$("#requestHumanHelp").onclick = () => {
  resetHumanHelpDialog();
  $("#humanHelpMessage").value = "";
  $("#humanHelpDialog").showModal();
};
$("#humanHelpCancel").onclick = () => $("#humanHelpDialog").close();
$("#humanHelpBack").onclick = () => resetHumanHelpDialog();
$("#humanHelpForm").onsubmit = async event => {
  event.preventDefault();
  const button = $("#humanHelpContinue");
  button.disabled = true;
  try {
    if (!pendingHumanHelpPreview) {
      const preview = await post("/api/human-help/preview", {
        sessionId: selectedSessionId,
        purpose: $("#humanHelpPurpose").value.trim(),
        message: $("#humanHelpMessage").value.trim(),
      });
      showHumanHelpPreview(preview);
      return;
    }
    if (!$("#humanHelpConsent").checked) throw Error("请先核对并同意本次发送的数据和接收方");
    const payload = await post("/api/human-help/submit", {
      sessionId: selectedSessionId,
      previewId: pendingHumanHelpPreview.previewId,
      accepted: true,
    });
    render(payload.state);
    $("#humanHelpDialog").close();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
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
