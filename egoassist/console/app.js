const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];
const esc = value => String(value ?? "").replace(/[&<>"']/g, character => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character]));
let state, stream, socket, audioContext, microphoneSource, processor, videoTimer;
let mediaCaptureClockId = null;
let livekitRoom = null, livekitAudioElements = [];
let interactionTransport = "websocket-fallback";
let playbackTime = 0, assistantText = "";
let captionResponseId = null, currentTurnId = null;
let speechGeneration = null;
const stoppedSpeechResponses = new Set();
const playbackAudit = new PlaybackAudit(payload => {
  if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(payload));
});
setInterval(() => { if (audioContext) playbackAudit.report(audioContext.currentTime); }, 500);
let detachedStatePolling = false, lastDetachedStatePoll = 0;
const turnOutbox = () => new TurnOutbox(sessionStorage, currentUser.id, selectedSessionId);
let connectingPromise = null;
let connectionGeneration = 0;
let providerReady = false;
let mediaPromise = null;
let mediaGeneration = 0;
let selectedEventId = null;
let currentUser = null, selectedSessionId = null, authMode = "login";
let sessionRevision = 0, sessionSwitchBusy = false;
const sessionDrafts = new Map();
function sessionScope() {
  const revision = sessionRevision, uid = currentUser?.id, sid = selectedSessionId;
  return () => revision === sessionRevision && uid === currentUser?.id && sid === selectedSessionId;
}
let sessionCatalog = [], memoryPolicySaving = false;
// Keep a submitted text turn visible while the next state snapshot is in flight.
// The server remains authoritative; entries are removed as soon as its ledger
// contains the same turn id.
const localSubmittedTurns = new Map();
let ambientState = null;
let insightSession = null, insightBusy = false, insightTurn = null, insightLastAttempt = 0;
let insightJobs = [], selectedInsightJob = null, insightPolling = false, displayedInsightResult = null;
let insightLastPoll = 0;
let insightResultRequest = 0;
const insightPhases = {queued: "等待执行名额", collecting: "读取原始记录", preparing: "准备报告证据",
  "waiting-provider": "等待模型响应", validating: "校验输出与来源", finished: "处理结束"};
let promptAuditSignature = "";
let promptAuditPolling = false;
let promptAuditSession = null;
let promptAuditCursor = null;
const promptAuditRows = new Map();
let stateRefreshPendingAt = 0;
let stateSyncRevision = 0;
let pdfjs = null, artifactDocument = null, artifactPageNumber = 1, artifactScale = 1;
let artifactRenderGeneration = 0, artifactRenderTask = null, pendingHumanHelpPreview = null;
let mirrorCamera = localStorage.getItem("egoassist.camera.mirror") !== "false";
let microphoneEnabled = localStorage.getItem("egoassist.microphone.enabled") !== "false";
const playbackSources = new Set();
const observationResponseKinds = new Map();
const AUDIO_BUFFER_HIGH_WATER = 512 * 1024;
const VIDEO_BUFFER_HIGH_WATER = 128 * 1024;
let droppedAudioChunks = 0;
let droppedVideoFrames = 0;
const responseScopes = new WeakMap();
const toolActivityView = new ToolActivityView($("#toolActivity"));
async function request(path, options = {}) {
  const isCurrent = sessionScope();
  try {
    const result = await fetchJSON(path, options);
    if (result && typeof result === "object") {
      responseScopes.set(result, isCurrent);
      if (result.state && typeof result.state === "object") responseScopes.set(result.state, isCurrent);
    }
    return result;
  } catch (error) {
    if (error.status === 401 && isCurrent()) showLogin();
    throw error;
  }
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
  $("#authTitle").textContent = registering ? "注册账号" : "登录账号";
  $("#authSubmit").textContent = registering ? "注册并进入" : "登录";
  $("#authPassword").autocomplete = registering ? "new-password" : "current-password";
  $("#authError").textContent = "";
}

function showLogin() {
  $("#stateConnectionStatus").textContent = "";
  window.resetConversationHistory?.();
  sessionRevision += 1;
  disconnectRealtime();
  currentUser = null;
  selectedSessionId = null;
  insightSession = null;
  insightJobs = [];
  selectedInsightJob = null;
  $("#insightJobs").replaceChildren();
  insightTurn = null;
  state = null;
  ambientState = null; ambientScope = null; ambientPanel.reset();
  observationResponseKinds.clear();
  localSubmittedTurns.clear();
  sessionDrafts.clear();
  toolActivityView.reset(null);
  $("#textTurnInput").value = "";
  $("#textTurnSend").disabled = false;
  $("#proactiveSuggestions").checked = false;
  $("#insightResults").replaceChildren();
  $("#actualPromptRequests").replaceChildren();
  promptAuditRows.clear(); promptAuditSession = null; promptAuditCursor = null;
  $("#memoryRecoveryJobs").replaceChildren();
  $("#modelConfiguration").textContent = "";
  $("#promptAuditCount").textContent = "";
  $("#olderPromptRequests").hidden = true;
  document.body.classList.add("auth-required");
  $("#authGate").hidden = false;
  $("#authPassword").value = "";
}

async function refreshSessionList(preferredId = selectedSessionId) {
  const isCurrent = sessionScope();
  const payload = await request("/api/sessions");
  if (!isCurrent()) return;
  let sessions = payload.sessions || [];
  if (!sessions.length) {
    const created = await post("/api/sessions");
    if (!isCurrent()) return;
    preferredId = created.session.id;
    sessions = (await request("/api/sessions")).sessions || [];
    if (!isCurrent()) return;
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
  const isCurrent = sessionScope(), sid = selectedSessionId;
  memoryPolicySaving = true;
  renderMemoryPolicy(before, state?.memoryProfiles || []);
  try {
    const result = await post("/api/sessions/memory-policy", {sessionId: sid, policy});
    if (!isCurrent()) return;
    const session = sessionCatalog.find(item => item.id === sid);
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
  try {
    if (turnOutbox().entries().length || state?.workLedger?.turnInbox?.some(turn =>
        ["received", "deferred", "classifying", "ready", "processing"].includes(turn.status))) {
      $("#textTurnError").textContent = "正在恢复未确认的文本请求";
      connectRealtime({media: false}).catch(error => { $("#textTurnError").textContent = error.message; });
    }
  } catch (error) { $("#textTurnError").textContent = error.message; }
}

function renderPrivacy(payload) {
  if (!payload) return;
  if (payload.ambient) {
    acceptAmbient({...payload.ambient, ...(state?.ambient || {})});
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
  const response = await fetch("/api/privacy/export", {signal: AbortSignal.timeout(60000)});
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
  const intent = state.workLedger?.intents?.find(i => i.intentId === item.intentId);
  const turnId = item.payload?.turnId || item.detail?.turnId || item.turnIds?.[0] || intent?.turnIds?.[0];
  const turn = state.workLedger?.turnInbox?.find(t => t.turnId === turnId);
  return semantic.summary || semantic.input || item.payload?.text || turn?.text || item.skillRef?.task || workKindLabel(fallback);
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
  if (!state) return;
  const ledger = state.workLedger || {intents: [], jobs: [], resultInbox: [], foregroundIntentId: null};
  const turnStates = {"pending-send": "已保留，等待服务端接收", received: "已接收，等待处理", responding: "正在回答", completed: "回答已完成",
    interrupted: "回答已打断", classifying: "正在分析问题", processing: "正在处理", ready: "等待执行",
    deferred: "排队中", failed: "处理失败", cancelled: "已取消"};
  const serverTurns = ledger.turnInbox || [];
  serverTurns.forEach(turn => localSubmittedTurns.delete(turn.turnId));
  const turns = [...serverTurns, ...[...localSubmittedTurns.values()].filter(turn => !serverTurns.some(item => item.turnId === turn.turnId))]
    .sort((a, b) => (a.sequence || 0) - (b.sequence || 0));
  const history = $("#turnWorkHistory");
  const historySessionChanged = history.dataset.session !== selectedSessionId;
  const followLatest = historySessionChanged || history.scrollHeight - history.scrollTop - history.clientHeight < 48;
  const historyScroll = history.scrollTop;
  history.dataset.session = selectedSessionId;
  const historyHTML = turns.map(turn => {
    const status = turn.delivery?.status || turn.status;
    const answer = turn.answer;
    const preparation = state.runtimeLanes?.interaction?.preparation?.find(row => row.turnId === turn.turnId);
    const answerLabels = {queued: "后台补全排队中", running: "正在后台补全", paused: "已暂停计算", failed: "后台补全失败", cancelled: "已取消或被更正",
      completed: answer?.channel === "background-text" ? "已在后台回答" : status === "completed" && turn.delivery?.channel === "device-playback" ? "设备已播完" : "已生成 · 播放未确认"};
    const label = answer?.phase === "waiting-corrections" && answer.status === "queued" ? "答案已准备 · 等待当前话语是否有纠正" : turn.delivery?.channel === "memory-receipt" && turn.delivery?.memoryStatus === "committed" ? "记忆已保存" : !answer && turn.status === "completed" && turn.semantic?.interaction?.action === "listen" ? "现场对话已记录，无需助手回答" : answer && answer.status !== "foreground" ? answerLabels[answer.status]
      : !turn.delivery && status === "completed" ? "已处理，查看任务结果" : turnStates[status] || status;
    return "<div class='turn-history-row' data-turn-id='" + esc(turn.turnId) + "'><p>" + esc(turn.text) +
      "</p><small>" + esc(label) + "</small>" +
      (preparation ? "<small>" + esc(interactionPhases[preparation.phase] || preparation.phase) +
        " · 本阶段 " + Math.max(0, (Date.now() - Date.parse(preparation.updatedAt)) / 1000).toFixed(1) + " s" +
        (preparation.queueMs != null ? " · 排队 " + esc(preparation.queueMs) + " ms" : "") + "</small>" : "") +
      (answer?.text && answer.status === "completed" ? "<p class='completed-answer'>" + esc(answer.text) + "</p>" : "") +
      (answer && ["queued", "running", "failed"].includes(answer.status) ? "<small>" + esc(answer.model +
        (answer.status === "running" ? " · 已等待 " + Math.max(0, (Date.now() - Date.parse(answer.startedAt)) / 1000).toFixed(1) +
        " s · 请求上限 " + answer.timeoutSeconds + " s" : "") + (answer.error ? " · " + answer.error : "")) + "</small>" : "") +
      (answer?.durationMs != null ? "<small>回答耗时 " + esc(answer.durationMs) + " ms</small>" : "") +
      (answer?.inputToCompleteMs != null ? "<small>接收到完整答案 " + esc(answer.inputToCompleteMs) + " ms</small>" : "") +
      (() => {
        const models = [...new Set([
          turn.semantic?.model,
          ...(state.events || []).filter(e => e.payload?.turnId === turn.turnId || e.payload?.origin?.turnId === turn.turnId)
            .flatMap(e => [e.model, e.payload?.model, e.payload?.request?.model]),
          answer?.model,
        ].filter(Boolean))];
        return models.length ? "<small>经手模型：" + esc(models.join(" → ")) + "</small>" : "";
      })() +
      "<button type='button' class='inspect-turn' data-inspect-turn='" + esc(turn.turnId) + "'>查看处理记录</button>" +
      (answer && ["queued", "running"].includes(answer.status) ? "<button type='button' data-answer-action='cancel' data-answer-turn='" + esc(turn.turnId) + "'>取消这个问题</button>" : "") +
      (answer?.status === "failed" ? "<button type='button' data-answer-action='retry' data-answer-turn='" + esc(turn.turnId) + "'>重试回答</button>" : "") +
      (!answer && ["interrupted", "failed"].includes(status) ? "<button type='button' data-reask-turn='" + esc(turn.turnId) + "'>重新提问</button>" : "") + "</div>";
  }).join("");
  if (history.innerHTML !== historyHTML) {
    history.innerHTML = historyHTML;
    history.scrollTop = followLatest ? history.scrollHeight : historyScroll;
  }
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
    ? "重启后仍保留"
    : "提醒服务尚未就绪";
  $("#requestHumanHelp").disabled = humanHelpCapability.status !== "available";
  $("#humanHelpAvailability").textContent = humanHelpCapability.status === "available"
    ? "发送前需核对内容" : "尚未配置接收方";
  $("#capabilityTruth").innerHTML = capabilities.map(item =>
    "<span class='capability-state " + esc(item.status) + "' title='" + esc(item.reason) + "'><b>" +
    esc(item.label + " · " + workStatusLabel(item.status)) + "</b></span>"
  ).join("") || "<span class='muted'>能力真值尚未加载</span>";

  const activeJobs = ledger.jobs.filter(item => !terminalWorkStatus(item.status));
  const pendingResults = ledger.resultInbox.filter(item => !["delivered", "dismissed"].includes(item.status));
  $("#workCenterCounts").textContent = activeJobs.length + " 项进行中 · " + pendingResults.length + " 项待确认结果";

  $("#intentList").innerHTML = ledger.intents.length ? [...ledger.intents].reverse().map(item => {
    const focused = item.intentId === ledger.foregroundIntentId;
    const canFocus = !focused && !terminalWorkStatus(item.status) && item.detail?.runtimeCheckpoint;
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(workLabel(item, item.kind)) +
      "</b><small>" + esc(workKindLabel(item.kind) + " · " + workStatusLabel(item.status)) + "</small></div>" +
      (canFocus ? "<button data-focus-intent='" + esc(item.intentId) + "'>切到前台</button>" : focused ? "<b>前台</b>" : "") +
      "</article>";
  }).join("") : "<p class='empty-state'>暂无已记录意图</p>";

  $("#jobList").innerHTML = activeJobs.length ? [...activeJobs].reverse().map(item => {
    const cancellable = !terminalWorkStatus(item.status);
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(workLabel(item, item.kind)) +
      "</b><small>" + esc(workKindLabel(item.kind) + " · " + workStatusLabel(item.status)) + "</small></div>" +
      (cancellable ? "<button class='danger' data-cancel-job='" + esc(item.jobId) + "'>取消</button>" : "") +
      "</article>";
  }).join("") : "<p class='empty-state'>当前没有后台待办</p>";

  $("#resultInbox").innerHTML = ledger.resultInbox.length ? [...ledger.resultInbox].reverse().map(item => {
    const content = readableResult(item.content);
    const dismissible = !["delivered", "dismissed"].includes(item.status);
    const artifact = item.content?.artifact || item.content?.result?.artifact;
    return "<article class='work-row " + esc(item.status) + "'><div><b>" + esc(content) +
      "</b><details><summary>原始结果</summary><pre>" + esc(JSON.stringify(item.content, null, 2)) + "</pre></details><small>" + esc(workStatusLabel(item.status)) + "</small></div>" +
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


function renderPerformance(audit = {}) {
  const labels = {
    routerMs: ["语义路由", "云端结构化请求耗时"],
    retrievalMs: ["记忆检索", "本地检索与证据编排耗时"],
    writePolicyMs: ["记忆写入", "本地写入策略与持久化耗时"],
    responseFirstTextMs: ["响应首文本", "从响应请求到供应商首文本；不含此前路由"],
    responseFirstAudioMs: ["响应首音频", "从响应请求到供应商首音频；不含此前路由、麦克风端点检测或播放"],
    insightMs: ["报告与建议", "读取来源、云端生成与引用校验耗时"],
  };
  $("#performanceWindow").textContent = "最近 " + (audit.windowEvents || 0) + " 个事件 · 超时 " + (audit.timeouts || 0);
  $("#performanceMetrics").innerHTML = Object.entries(labels).map(([key, [label, meaning]]) => {
    const metric = audit.metrics?.[key];
    const detail = metric ? [metric.timestamp, "event " + metric.eventId,
      metric.responseId ? "response " + metric.responseId : "", metric.samples ? "n=" + metric.samples + ", median=" + metric.median + " ms" : ""].filter(Boolean).join(" · ") : "暂无测量";
    return "<div class='performance-metric' title='" + esc(meaning + " · " + detail) + "'><span>" + esc(label) +
      "</span><b data-performance='" + key + "'>" + (metric ? Number(metric.value).toFixed(1) + " ms" : "-") +
      "</b><small>" + esc(metric?.timestamp ? new Date(metric.timestamp).toLocaleTimeString() : "未测量") + "</small></div>";
  }).join("");
  const usage = audit.latestUsage;
  $("#performanceUsage").textContent = usage ? "最近响应 " + (usage.responseId || "-") + " · 输入 " +
    (usage.inputTokens ?? "-") + " / 输出 " + (usage.outputTokens ?? "-") + " tokens · " + (usage.status || "-") : "暂无响应用量";
}

function render(next) {
  if (!currentUser || !next || next.sessionId !== selectedSessionId) return;
  if (responseScopes.has(next) && !responseScopes.get(next)()) return;
  stateSyncRevision++;
  $("#stateConnectionStatus").textContent = "";
  state = next;
  if (state.ambient) {
    acceptAmbient(state.ambient); state.ambient = ambientState;
    if(state.runtimeLanes?.observation)state.runtimeLanes.observation.enabled=Boolean(ambientState.enabled && ambientState.consented);
  }
  toolActivityView.snapshot(selectedSessionId, state.toolActivity);
  renderSharedScene(state.sharedScene);
  renderPerformance(state.performance);
  syncInsightSession();
  maybeSuggest();
  const task = state.task, memory = state.memory;
  const interaction = state.interaction;
  $("#outputPermission").textContent = interaction?.paused ? "暂让声音 · 等待意图" : interaction?.quiet
    ? "免打扰" + (interaction.until ? " · 至 " + new Date(interaction.until).toLocaleTimeString() : " · 等待解除")
      + (interaction.allowReplies ? " · 可回答主动提问" : " · 静默") : "可播报";
  const decision = task?.decision || state.genericDecision;
  const step = task ? task.steps.find(item => item.id === task.currentCheckpointId) || task.steps[task.stepIndex] : null;
  $("#mode").textContent = "● " + state.realtimeModel;
  $("#sessionId").textContent = state.sessionId;
  $("#promptHash").textContent = state.promptContext.hash;
  $("#promptContext").textContent = state.promptContext.text;
  const conversational = state.activeSkill?.workflowMode === "conversational";
  $("#skillName").textContent = state.activeSkill ? state.activeSkill.name + " / " + (task ? task.task.title : conversational ? "对话工作流" : "待确认任务") : "通用对话";
  $("#taskTitle").textContent = task ? task.task.title + " · " + task.task.version : conversational ? "对话工作流已就绪" : "No task hypothesis";
  $("#skillSelect").innerHTML = "<option value=''>通用模式 · 按意图加载</option>" + state.skills.map(item => "<option value='" + esc(item.name) + "'>" + esc(item.name) + " · " + (item.workflowMode === "conversational" ? "对话工作流" : item.tasks.length + " tasks") + "</option>").join("");
  $("#skillSelect").value = state.activeSkill?.name || "";
  $("#taskSelect").innerHTML = state.tasks.length ? "<option value=''>选择具体任务</option>" + state.tasks.map(item => "<option value='" + esc(item.id) + "'>" + esc(item.title) + " · " + esc(item.provenance) + "</option>").join("") : "<option value=''>无活动任务</option>";
  $("#taskSelect").value = task?.task.id || "";
  $("#taskSelect").disabled = !state.tasks.length;
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
  $("#decision").innerHTML = "<h3>" + esc(decision.message) + "</h3><p>" + esc(decision.reason) + "</p>";
  $("#mapStatus").textContent = task ? "当前假设 " + step.id + " · " + task.steps.length + " checkpoints" : conversational ? "无固定步骤" : "等待意图与任务证据";
  $("#steps").innerHTML = task ? task.steps.map(item => {
    const label = item.id === task.currentCheckpointId ? "当前系统假设" : "可选证据锚点";
    return "<article class='checkpoint " + esc(item.status) + "'><span>" + esc(item.status) + "<br>" + esc(item.updatedBy || "未确认") + "</span><div><h3>" + esc(item.title) + "</h3><p>" + esc(label + " · " + item.instruction) + "</p></div><b>evidence " + (item.evidenceIds || []).length + "</b></article>";
  }).join("") : conversational ? "<p class='empty-state'>领域指令已加载</p>" : "<p class='empty-state'>尚未建立任务假设。意图路由匹配后才加载领域工作流。</p>";
  $("#knowledge").innerHTML = "<b>" + esc(state.knowledgeMechanism.method) + "</b><br>selected: " + esc(step ? step.id + " / " + step.source : state.activeSkill?.name || "none") + "<br><span>边界：" + esc(state.knowledgeMechanism.claimBoundary) + "</span>";
  const latestRejection = task?.perception?.audit?.[0];
  $("#vision").textContent = task?.observation
    ? task.observation.step + " · accepted · age " + task.observation.frameAgeMs + " ms"
    : latestRejection ? "证据未采纳 · " + latestRejection.reason : stream && state.sharedScene?.camera === "receiving" ? "现场画面接收中" : "";
  $("#memoryScope").textContent = state.memoryPolicy?.components?.timeline?.enabled ? "记忆已开启" : "记忆已关闭";
  renderMemoryPolicy(state.memoryPolicy, state.memoryProfiles || []);
  const episodic = state.episodicMemory || {recent: [], retrieval: {candidates: []}};
  renderTimelineCards(episodic);
  $("#toolAudit").innerHTML = state.toolAudit.length ? state.toolAudit.map(item => "<article><b>" + esc(item.tool || item.action || "tool") + "</b><p>" + esc(JSON.stringify(item.arguments || item.result || {})) + "</p><small>" + esc(item.time || item.timestamp || "") + "</small></article>").join("") : "<span class='muted'>暂无工具调用</span>";
  $("#memoryCandidate").innerHTML = memory.candidate ? "<div class='candidate'><b>要记住这个提示偏好吗？</b><p>" + esc(memory.candidate.preference) + "</p><button data-accept='true'>确认保存</button><button data-accept='false'>不要保存</button></div>" : "";
  $$("[data-accept]").forEach(button => button.onclick = () => runMemoryMutation(button, post("/api/candidate", {accepted: button.dataset.accept === "true", sessionId: selectedSessionId})));
  const scheduler = state.scheduler;
  $("#scheduler").innerHTML = "<b>Response scheduler</b><br>active: " + esc((scheduler.activeResponseIds || []).join(", ") || "none") + "<br>queued: " + esc(JSON.stringify(scheduler.queued)) + "<br>inflight: " + esc(JSON.stringify(scheduler.inflight)) + "<br>barge-in pending: " + esc(scheduler.bargeInPending) + "<br><b>Policy source</b>: " + esc(state.turnTakingPolicy.source) + "<br>selected: " + esc(JSON.stringify(state.turnTakingPolicy.currentSelection)) + "<br>protected max: " + esc(state.turnTakingPolicy.maxProtectionMs) + " ms";
  $("#intentRouting").textContent = JSON.stringify(state.intentRouting, null, 2);
  $("#audioAttribution").textContent = JSON.stringify(state.audioAttribution, null, 2);
  renderWorkCenter();
  renderTurnWorkflow();
  renderTimeline(state.events || []);
  renderPerceptionEvidence(task);
  renderAmbient();
  renderExecutionLanes();
}

const interactionPhases = {queued:"请求已保留 · 等待处理名额", classifying:"正在理解你的请求", ready:"理解完成 · 等待按序处理", retrying:"理解请求暂未成功 · 正在重试",
  "waiting-vision":"正在核对提问时的画面", "waiting-speech-gap":"回答已准备 · 等你说完", "text-fallback":"语音暂未接上 · 答案保留在记录中", committing:"正在检索记忆与处理请求", generating:"正在生成回答"};

function renderExecutionLanes() {
  window.LaneView.render(state);
}

// Ambient observation must be visible for as long as it is running. ADR 005/007:
// a camera that watches without a task the user started is only acceptable if the
// user can see, at a glance, that it is on and what it is watching for.
function renderObservationReviews() {
  const reviews = (state?.runtimeLanes?.observation?.reviews || []).filter(row => row.question || row.notification || row.rawNotification || row.referenceCheck).slice().reverse();
  if (!reviews.length) return "";
  const statuses = {recorded:"已记录比对", suppressed:"本次不播报", deferred:"暂缓播报", ready:"等待播报音频", "audio-sent":"音频已发送 · 播放待确认", played:"设备已开始播放", closed:"信息确认已结束"};
  const reasons = {"invalid-reference-schema":"比对格式不完整，保留原始观察", "unknown-reference":"引用不在本次背景中", "reference-recorded-after-frame":"引用晚于画面时间",
    "same-frame-is-not-independent-evidence":"同一帧的重复分析不能独立佐证", "unknown-visible-evidence":"缺少对应画面证据",
    "unknown-follow-up":"无法定位此前事项", "already-offered":"同一事项已播报", "stale-frame":"画面已过实时期限",
    "invalid-notification-schema":"提醒格式不完整", "missing-user-need":"缺少用户需要的依据", "missing-user-request":"缺少提醒请求",
    "reminders-disabled":"主动提醒已关闭", "reminder-goal-changed":"提醒目标已修改或撤销", "already-queued":"同一事项已在等待播报",
    "useful-reminder-at-idle":"根据当前需要在空闲时提醒", "delivery-not-confirmed":"上次播报未确认，等待新画面核对",
    "questions-disabled":"主动询问已关闭", "output-not-permitted":"当前不允许主动播报", "pending-clarification":"还有问题等待确认",
    "interaction-busy":"等待当前对话结束", cooldown:"尚在主动播报间隔内", "no-material-change":"没有值得再问的新变化",
    "useful-question-at-idle":"空闲时提出有用的问题", "awaiting-device-playback":"等待设备播放回执",
    "device-playback-confirmed":"设备已确认播放", "clarification-no-longer-pending":"待确认事项已处理或过期",
    "output-interrupted-before-audio":"播报尚未发送便被打断", "no-useful-question":"本次无需主动播报"};
  const relations = {conflict:"疑似不一致", change:"状态变化", uncertain:"尚不能判定", consistent:"本次相符"};
  return "<details data-disclosure='observation-reviews'><summary>提醒、询问与依据</summary>" + reviews.map(row =>
    "<div class='observation-review'><b>" + esc(statuses[row.status] || row.status) + "</b> · " + esc(reasons[row.reason] || row.reason) +
    (row.question ? "<p>" + esc(row.question) + "</p>" : "") +
    (row.notification ? "<p>提醒：" + esc(row.notification.message) + "</p><p>此时提醒的原因：" + esc(row.notification.reason) + "</p>" : "") +
    (row.referenceCheck ? "<p>" + esc(relations[row.referenceCheck.relation] || row.referenceCheck.relation) + "：" + esc(row.referenceCheck.reason) + "</p>" : "") +
    "<small>画面时间 " + esc(new Date(row.capturedAt).toLocaleString()) + "</small>" +
    "<details data-disclosure='review-" + esc(row.candidateId) + "'><summary>来源、校验与原始记录</summary><pre>" + esc(JSON.stringify(row, null, 2)) + "</pre></details></div>"
  ).join("") + "</details>";
}

const ambientPanel = new AmbientPanel($("#ambientBanner"), {
  session: () => selectedSessionId,
  consent: setAmbientConsent,
  connect: async () => {
    const isCurrent = sessionScope();
    // Unlock output in the button gesture even when no microphone is requested.
    audioContext = audioContext || new AudioContext();
    await audioContext.resume();
    if(!isCurrent())return;
    await connectRealtime({media:false});
    if(isCurrent())render(await request(selectedStatePath()));
  },
  settings: async settings => {
    const isCurrent = sessionScope();
    const payload = await post("/api/ambient/settings", {sessionId:selectedSessionId, ...settings});
    if (!isCurrent()) return;
    acceptAmbient(payload.ambient || payload.state?.ambient);
    render(payload.state);
  },
  devices: () => {
    location.hash = "/conversation";
    showWorkspaceView();
    $("#cameraPanel").open = true;
    $("#cameraPanel").scrollIntoView({block:"nearest"});
    $("#cameraButton").focus({preventScroll:true});
  }
});
let ambientScope = null;
function acceptAmbient(info) {
  if (!info) return;
  const scope = currentUser?.id + ":" + selectedSessionId;
  if (ambientScope !== scope) { ambientScope=scope; ambientState=null; }
  const sameEpoch = info.controlEpoch && ambientState?.controlEpoch === info.controlEpoch;
  if (sameEpoch && (info.controlRevision ?? -1) < (ambientState?.controlRevision ?? -1)) return;
  // Partial provider messages are evidence only, never authoritative control state.
  if (info.controlRevision == null && ambientState?.consented === false) return;
  const previousGoal = ambientState?.reminderGoal;
  ambientState = {...(ambientState || {}), ...info};
  const disabled = new Set();
  if (!observationOutputAllowed('clarification')) disabled.add('clarification');
  if (!observationOutputAllowed('reminder') || (previousGoal !== undefined && previousGoal !== ambientState.reminderGoal)) disabled.add('reminder');
  if (disabled.size) stopObservationPlayback(disabled);
}
function renderAmbient() {
  ambientPanel.render(ambientState, currentUser?.id + ":" + selectedSessionId, renderObservationReviews());
}
async function setAmbientConsent(granted) {
  const isCurrent = sessionScope();
  const payload = await post("/api/ambient/consent", {granted, sessionId:selectedSessionId});
  if (!isCurrent()) return;
  acceptAmbient(payload.ambient);
  if (payload.state) render(payload.state);
  else renderAmbient();
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
    mediaCaptureClockId = crypto.randomUUID();
    stream.getAudioTracks().forEach(track => { track.enabled = microphoneEnabled; });
    $("#video").srcObject = stream;
    applyCameraOrientation();
    $("#cameraEmpty").hidden = true;
    $("#cameraState").textContent = microphoneEnabled ? "摄像头与麦克风在线" : "摄像头在线 · 麦克风已关闭";
    $("#cameraButton").textContent = "关闭设备";
    $("#cameraPanel").open = true;
    if (socket?.readyState === WebSocket.OPEN) {
      await activateWebSocketMediaFallback();
      startLocalVad(stream);
      sendMediaConfiguration(socket);
    }
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
  stopAudioCapture();
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

async function connectLiveKitMedia(connectionSessionId, generation) {
  if (!window.LivekitClient) return false;
  const capturedStream = stream;
  const isCurrent = () => generation === connectionGeneration && stream === capturedStream;
  let credentials;
  try {
    credentials = await post("/api/livekit/token", {sessionId: connectionSessionId});
  } catch (_) {
    return false;
  }
  if (!credentials.enabled || !isCurrent()) return false;
  const {Room, RoomEvent, Track} = window.LivekitClient;
  const room = new Room({adaptiveStream: true, dynacast: true, disconnectOnPageLeave: true});
  room.on(RoomEvent.TrackSubscribed, track => {
    if (!isCurrent() || track.kind !== Track.Kind.Audio) return;
    const element = track.attach();
    element.autoplay = true;
    element.dataset.egoassistLivekitAudio = "true";
    document.body.appendChild(element);
    livekitAudioElements.push(element);
  });
  room.on(RoomEvent.TrackUnsubscribed, track => track.detach().forEach(element => element.remove()));
  room.on(RoomEvent.Reconnecting, () => {
    if (!isCurrent()) return;
    $("#duplexState").textContent = "LiveKit 媒体正在重连，控制通道保持在线";
  });
  room.on(RoomEvent.Reconnected, () => {
    if (!isCurrent()) return;
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
    if (!isCurrent()) { await room.disconnect(); return false; }
    const audioTrack = capturedStream?.getAudioTracks()[0];
    const videoTrack = capturedStream?.getVideoTracks()[0];
    if (audioTrack) {
      await room.localParticipant.publishTrack(audioTrack, {source: Track.Source.Microphone});
    }
    if (!isCurrent()) { await room.disconnect(); return false; }
    if (videoTrack) {
      await room.localParticipant.publishTrack(videoTrack, {source: Track.Source.Camera});
    }
  } catch (_) {
    await room.disconnect().catch(() => {});
    return false;
  }
  if (!isCurrent()) { await room.disconnect().catch(() => {}); return false; }
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
  if (processor || !stream) return;
  const capturedStream = stream, generation = connectionGeneration;
  audioContext = audioContext || new AudioContext();
  await audioContext.resume();
  if (processor || stream !== capturedStream || generation !== connectionGeneration) return;
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
    const endMonotonicMs = performance.now();
    socket.send(JSON.stringify({type: "audio", audio, capture: {
      clockId: mediaCaptureClockId, startMonotonicMs: endMonotonicMs - event.inputBuffer.duration * 1000,
      endMonotonicMs, basis: "audio-callback-estimate", capturedAt: new Date().toISOString()
    }}));
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
  const capturedAt = new Date().toISOString(), monotonicMs = performance.now();
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
  socket.send(JSON.stringify({type: "video", image, capture: {capturedAt, monotonicMs,
    clockId: mediaCaptureClockId, basis: "camera-sampling-estimate",
    mirrored: mirrorCamera, width: canvas.width, height: canvas.height}}));
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

function playPcm24(base64Audio, responseId) {
  if (!audioContext) return;
  const binary = atob(base64Audio), pcm = new Int16Array(Math.floor(binary.length / 2));
  for (let index = 0; index < pcm.length; index++) pcm[index] = binary.charCodeAt(index * 2) | (binary.charCodeAt(index * 2 + 1) << 8);
  const buffer = audioContext.createBuffer(1, pcm.length, 24000);
  const channel = buffer.getChannelData(0);
  for (let index = 0; index < pcm.length; index++) channel[index] = pcm[index] / 32768;
  const source = audioContext.createBufferSource();
  source.buffer = buffer;
  source.egoassistResponseId = responseId;
  source.connect(audioContext.destination);
  const now = audioContext.currentTime;
  playbackTime = Math.max(playbackTime, now + 0.03);
  source.start(playbackTime);
  playbackAudit.queued(responseId, source, playbackTime, 24000);
  playbackSources.add(source);
  source.onended = () => playbackSources.delete(source);
  playbackTime += buffer.duration;
}

function observationOutputAllowed(kind) {
  return Boolean(ambientState?.consented && (kind === 'reminder' ? ambientState.remindersEnabled !== false : ambientState.questionsEnabled));
}
function stopObservationPlayback(kinds = new Set(['reminder','clarification'])) {
  const matches = id => observationResponseKinds.has(id) && kinds.has(observationResponseKinds.get(id));
  for (const [id, kind] of observationResponseKinds) if (kinds.has(kind)) stoppedSpeechResponses.add(id);
  if (matches(captionResponseId)) {
    livekitAudioElements.forEach(element => { element.muted = true; });
  }
  for (const source of [...playbackSources]) {
    if (!matches(source.egoassistResponseId)) continue;
    if(audioContext)playbackAudit.report(audioContext.currentTime,"paused",source.egoassistResponseId);
    try { source.stop(); } catch (_) {}
    playbackSources.delete(source);
  }
  if (!playbackSources.size) playbackTime = audioContext ? audioContext.currentTime : 0;
}

function stopPlayback() {
  if (audioContext) playbackAudit.report(audioContext.currentTime, "paused");
  livekitAudioElements.forEach(element => { element.muted = true; });
  playbackSources.forEach(source => { try { source.stop(); } catch (_) {} });
  playbackSources.clear();
  playbackTime = audioContext ? audioContext.currentTime : 0;
}

async function connectRealtime({media = true, retryAttempt = 0} = {}) {
  if (socket && socket.readyState === WebSocket.OPEN && providerReady && !connectingPromise) {
    if (media && !stream) await ensureMedia();
    return;
  }
  if (connectingPromise) return connectingPromise;
  const generation = ++connectionGeneration;
  if (socket) socket.close();
  providerReady = false;
  const attempt = connectRealtimeSession(generation, media, retryAttempt);
  connectingPromise = attempt;
  try { await attempt; }
  finally { if (connectingPromise === attempt) connectingPromise = null; }
}

async function connectRealtimeSession(generation, media, retryAttempt) {
  if (!selectedSessionId) await refreshSessionList();
  if (generation !== connectionGeneration) return;
  const connectionSessionId = selectedSessionId;
  if (media) await ensureMedia();
  if (generation !== connectionGeneration) return;
  const livekitConnected = media && await connectLiveKitMedia(connectionSessionId, generation);
  if (generation !== connectionGeneration) return;
  if (media && stream) startLocalVad(stream);
  if (media && !livekitConnected) await startAudioCapture();
  if (generation !== connectionGeneration) return;
  const scheme = location.protocol === "https:" ? "wss:" : "ws:";
  const ws = new WebSocket(scheme + "//" + location.host + "/ws/realtime?session=" + encodeURIComponent(connectionSessionId));
  if (generation !== connectionGeneration) {
    ws.close();
    return;
  }
  socket = ws;
  stateRefreshPendingAt = 0;
  let ready = false;
  let restartAfterClose = false;
  let providerUnavailable = false;
  let connectionStage = "本地服务";
  let resolveReady, rejectReady;
  const readyPromise = new Promise((resolve, reject) => { resolveReady = resolve; rejectReady = reject; });
  const connectTimer = setTimeout(() => {
    if (!ready && generation === connectionGeneration) {
      rejectReady(Error("实时连接超时：" + connectionStage + "未能在 60 秒内就绪，请重试"));
      ws.close(4000, "ready-timeout");
    }
  }, 60000);
  readyPromise.then(() => clearTimeout(connectTimer), () => clearTimeout(connectTimer));
  ws.onopen = () => {
    if (socket !== ws || generation !== connectionGeneration) { ws.close(); return; }
    connectionStage = "实时模型连接或历史恢复";
    $("#duplexState").textContent = "正在连接实时模型…";
    if (stream && interactionTransport !== "livekit") {
      startAudioCapture().catch(() => {});
      const fps = Number($("#videoFps").value);
      if (videoTimer) clearInterval(videoTimer);
      videoTimer = setInterval(sendVideoFrame, 1000 / fps);
    }
  };
  ws.onmessage = event => {
    if (socket !== ws || generation !== connectionGeneration) return;
    const message = JSON.parse(event.data);
    if (message.origin?.observationReviewId && message.responseId) {
      const kind = message.origin.observationKind || 'clarification';
      observationResponseKinds.set(message.responseId, kind);
      while(observationResponseKinds.size>128)observationResponseKinds.delete(observationResponseKinds.keys().next().value);
      if (["audio","assistant_transcript"].includes(message.type) && !observationOutputAllowed(kind)) return;
    }
    if (message.type === "state") stateRefreshPendingAt = 0;
    if (["audio", "assistant_transcript"].includes(message.type) && stoppedSpeechResponses.has(message.responseId)) return;
    if (["audio", "assistant_transcript", "response_status"].includes(message.type) &&
        speechGeneration != null && message.origin?.generation != null &&
        message.origin.generation !== speechGeneration && !message.origin.background) return;
    if (message.type === "speech_control" && speechGeneration != null && message.generation != null && message.generation < speechGeneration) return;
    if (message.type === "memory_status" && message.turnId && currentTurnId && message.turnId !== currentTurnId) return;
    if (message.type === "connection_progress") {
      connectionStage = message.stage === "context-restore" ? "历史上下文恢复" : "云端实时模型连接";
      $("#duplexState").textContent = message.stage === "context-restore"
        ? "正在恢复历史会话…" : "正在连接实时模型…";
    } else if (message.type === "ready") {
      if (message.lanes?.dialogue?.connected === false || providerUnavailable) {
        rejectReady(Error("实时模型尚未连接"));
        ws.close(4000, "provider-not-ready");
        return;
      }
      ready = true;
      providerReady = true;
      speechGeneration = null; // A restarted server has a new connection-local fence.
      selectedSessionId = message.contextSessionId || selectedSessionId;
      try {
        const pending = turnOutbox().entries();
        if (pending.length) currentTurnId = pending.at(-1).clientRequestId;
        for (const item of pending) ws.send(JSON.stringify(item));
      } catch (error) { $("#textTurnError").textContent = error.message; }
      if (currentUser) localStorage.setItem("egoassist.session." + currentUser.id, selectedSessionId);
      refreshSessionList(selectedSessionId).catch(() => {});
      resolveReady();
      sendMediaConfiguration(ws);
      interactionTransport = message.interactionTransport === "livekit" && livekitRoom
        ? "livekit" : interactionTransport;
      $("#duplexState").textContent = stream ? (interactionTransport === "livekit" ? "LiveKit" : "WebSocket fallback") + " · 直接说话，可随时打断" : "文本会话在线";
      $("#realtimeButton").textContent = "结束语音对话";
    } else if (message.type === "provider_unavailable") {
      providerUnavailable = true;
      providerReady = false;
      rejectReady(Error(message.reason || "实时模型不可用"));
      $("#duplexState").textContent = "Provider offline · local console remains available";
      $("#assistantCaption").innerHTML = "<span>REALTIME PROVIDER UNAVAILABLE</span><p>" + esc(message.reason || "network or DNS unavailable") + "</p>";
    } else if (message.type === "audio" && interactionTransport !== "livekit" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
      $("#duplexState").textContent = "助手正在说 · 你可以随时打断";
      playPcm24(message.audio, message.responseId);
    } else if (message.type === "playback_resume") {
      stoppedSpeechResponses.delete(message.responseId);
      playbackAudit.resume(message.responseId, message.offsetSamples);
      currentTurnId = message.turnId;
      livekitAudioElements.forEach(element => { element.muted = false; });
      $("#duplexState").textContent = "正在继续原回答";
    } else if (message.type === "speech_floor") {
      if (speechGeneration != null && message.generation < speechGeneration) return;
      speechGeneration = message.generation;
      assistantText = "";
      captionResponseId = null;
    } else if (message.type === "playback_stop") {
      if (speechGeneration != null && message.generation != null && message.generation < speechGeneration) return;
      if (captionResponseId) stoppedSpeechResponses.add(captionResponseId);
      for (const id of message.responseIds || []) stoppedSpeechResponses.add(id);
      while (stoppedSpeechResponses.size > 128) stoppedSpeechResponses.delete(stoppedSpeechResponses.values().next().value);
      stopPlayback();
      $("#duplexState").textContent = "已停播 · 正在听你说";
    } else if (message.type === "speech_control") {
      if (!["resume", "keep"].includes(message.action)) stopPlayback();
      $("#duplexState").textContent = message.action === "keep" ? "已听到 · 保持当前回答" : message.action === "wait" ? "先听你说完" : message.action === "resume" ? "正在继续原回答" : message.action === "uncertain" ? "暂不播报 · 等待明确意图" : message.action === "cancel-answer" ? "已取消指定回答 · 正在听" : "已停止播报 · 正在听";
    } else if (message.type === "interaction_status") {
      if (message.turnId && state?.runtimeLanes?.interaction) {
        const lane = state.runtimeLanes.interaction;
        lane.preparation = (lane.preparation || []).filter(row => row.turnId !== message.turnId);
        lane.preparation.push({turnId:message.turnId, phase:message.status, queueMs:message.queueMs, updatedAt:message.updatedAt});
        renderWorkCenter();
      }
      if (!message.turnId || !currentTurnId || message.turnId === currentTurnId) {
        $("#duplexState").textContent = (interactionPhases[message.status] || message.status) +
          (message.queueMs != null ? " · 排队 " + message.queueMs + " ms" : "");
      }
    } else if (message.type === "interrupted") {
      if (speechGeneration != null && message.generation != null && message.generation !== speechGeneration) return;
      stopPlayback();
      assistantText = "";
      captionResponseId = null;
      $("#duplexState").textContent = "已打断旧响应，正在处理你的请求";
    } else if (message.type === "barge_in_policy") {
      $("#duplexState").textContent = message.action === "listening" ? "正在听 · 根据语义决定是否接话" : "正在处理语音交接";
    } else if (message.type === "user_transcript") {
      window.conversationHistoryEvent?.(message);
      if (message.channel === "explicit_control" && currentTurnId && message.turnId !== currentTurnId) return;
      currentTurnId = message.turnId || null;
      $("#transcript").textContent = (message.channel === "explicit_control" ? "你的文字" : "现场语音 · 说话人待核验") + "：" + message.text + (message.echoRisk === "suspected" ? " · 可能残余回声" : "");
      $("#duplexState").textContent = message.channel === "explicit_control" ? "正在处理文本" : "正在处理现场语音";
    } else if (message.type === "assistant_transcript" && ["conversation", "proactive-utterance"].includes(message.purpose)) {
      window.conversationHistoryEvent?.(message);
      livekitAudioElements.forEach(element => { element.muted = false; });
      if (captionResponseId !== message.responseId) assistantText = "";
      captionResponseId = message.responseId;
      assistantText = message.done ? message.text : assistantText + message.text;
      $("#assistantCaption").innerHTML = "<span>助手语音字幕 · " + (message.origin?.background ? "后台结果 · " : "") + (message.done ? "字幕已收齐" : "生成中") + "</span><p>" + esc(assistantText) + "</p>";
      $("#duplexState").textContent = message.done ? "字幕已收齐，等待模型结束响应" : "助手正在说 · 你可以随时打断";
      if (message.done) assistantText = "";
    } else if (message.type === "response_status") {
      window.conversationHistoryEvent?.(message);
      if (audioContext) playbackAudit.report(audioContext.currentTime, "generation-" + message.status, message.responseId);
      const turn = state?.workLedger?.turnInbox?.find(item => item.turnId === message.origin?.turnId);
      if (turn) {
        turn.delivery = {status: message.status === "completed" ? "generated" : message.status, responseId: message.responseId};
        renderWorkCenter();
      }
      if (message.status === "interrupted") {
        if (message.origin?.observationReviewId) stopObservationPlayback(new Set([message.origin.observationKind || 'clarification']));
        else stopPlayback();
        if (message.responseId === captionResponseId) {
          assistantText = "";
          captionResponseId = null;
        }
        $("#duplexState").textContent = "语音已中断 · 查看问题处理状态";
      } else if (message.status === "completed" && message.responseId === captionResponseId) {
        $("#duplexState").textContent = stream ? "全双工在线 · 正在听" : "文本会话在线";
      }
    } else if (message.type === "turn_receipt") {
      try { turnOutbox().acknowledge(message.turnId); }
      catch (error) { $("#textTurnError").textContent = error.message; return; }
      if (currentTurnId && message.turnId !== currentTurnId) return;
      $("#textTurnError").textContent = message.deduplicated ? "请求已受理，继续等待结果" : "请求已接收";
    } else if (message.type === "memory_status") {
      $("#assistantCaption").innerHTML = "<span>记忆事务 · 服务端确认</span><p>" + esc(message.message) + "</p>";
      $("#duplexState").textContent = message.status === "committed"
        ? (stream ? "记忆已提交 · 正在听" : "记忆已提交 · 文本会话在线")
        : message.status === "needs-input" ? "等待补充要记住的内容" : "记忆未提交";
    } else if (message.type === "tool_confirmation_required") {
      toolActivityView.event(selectedSessionId, message);
    } else if (message.type === "tool_status") {
      toolActivityView.event(selectedSessionId, message);
    } else if (message.type === "media_gap") {
      $("#duplexState").textContent = "Media gap detected; live capture continues";
    } else if (message.type === "performance_metric") {
      if (state && message.metric?.name) {
        state.performance = state.performance || {metrics: {}};
        state.performance.metrics = {...state.performance.metrics, [message.metric.name]: message.metric};
        renderPerformance(state.performance);
      }
    } else if (message.type === "state") {
      render(message.state);
    } else if (message.type === "ambient") {
      acceptAmbient(message.ambient);
      if (state) { state.ambient=ambientState; if(state.runtimeLanes?.observation)state.runtimeLanes.observation.enabled=Boolean(ambientState.enabled && ambientState.consented); }
      renderExecutionLanes();
      renderAmbient();
    } else if (message.type === "session_stale") {
      restartAfterClose = true;
      $("#duplexState").textContent = "任务已切换，正在重建实时会话…";
    } else if (message.type === "reconnecting") {
      $("#duplexState").textContent = "实时模型会话正在自动恢复 · 麦克风音频短暂缓冲中";
    } else if (message.type === "transport_status") {
      $("#duplexState").textContent = message.status === "connected"
        ? "LiveKit 媒体在线" : "LiveKit 媒体已断开，正在切换回退通道";
    } else if (message.type === "audio_unavailable") {
      $("#textTurnError").textContent = message.message;
    } else if (message.type === "error") {
      if (!ready) {
        rejectReady(Error(message.message || "实时模型启动失败"));
        ws.close(4000, "startup-error");
      }
      $("#textTurnError").textContent = message.message || "实时请求失败";
      $("#duplexState").textContent = "实时请求出错 · 查看输入区提示与处理记录";
    }
  };
  ws.onerror = () => { if (!ready && generation === connectionGeneration) rejectReady(Error("实时 WebSocket 连接失败")); };
  ws.onclose = event => {
    clearTimeout(connectTimer);
    if (generation !== connectionGeneration) { resolveReady(); return; }
    if (!ready && generation === connectionGeneration) rejectReady(Error("实时模型连接在就绪前关闭"));
    if (socket !== ws) return;
    $("#duplexState").textContent = "实时会话已结束";
    $("#realtimeButton").textContent = "开始语音对话";
    socket = null;
    stateRefreshPendingAt = 0;
    providerReady = false;
    stopPlayback();
    const nextAttempt = ready ? 1 : retryAttempt + 1;
    // Intentional disconnects invalidate generation before closing the socket.
    if ((restartAfterClose || !providerUnavailable) && nextAttempt <= 3 && currentUser) {
      $("#duplexState").textContent = "连接中断，正在恢复 · " + nextAttempt + "/3";
      setTimeout(() => {
        if (generation === connectionGeneration && connectionSessionId === selectedSessionId && currentUser) {
          connectRealtime({media: Boolean(stream), retryAttempt: nextAttempt}).catch(error => { $("#duplexState").textContent = error.message; });
        }
      }, Math.min(4000, 500 * 2 ** (nextAttempt - 1)));
    }
  };
  return readyPromise;
}

function stopAudioCapture() {
  stopLocalVad();
  if (processor) processor.disconnect();
  if (microphoneSource) microphoneSource.disconnect();
  processor = microphoneSource = null;
}

function disconnectRealtime() {
  if (audioContext) playbackAudit.report(audioContext.currentTime, "paused");
  playbackAudit.clear();
  stoppedSpeechResponses.clear();
  currentTurnId = null;
  speechGeneration = null;
  captionResponseId = null;
  assistantText = "";
  connectionGeneration += 1;
  connectingPromise = null;
  stateRefreshPendingAt = 0;
  providerReady = false;
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
  $("#realtimeButton").textContent = "开始语音对话";
}


$("#textTurnForm").onsubmit = async event => {
  event.preventDefault();
  const input = $("#textTurnInput"), button = $("#textTurnSend");
  const draft = input.value, transcript = draft.trim();
  if (!transcript || button.disabled || sessionSwitchBusy || !currentUser) return;
  const isCurrent = sessionScope();
  button.disabled = true;
  $("#textTurnError").textContent = providerReady ? "正在提交…" : "正在连接助手，输入已保留…";
  try {
    // Unlock the same speaker output during the user gesture. Resume in
    // parallel with connection setup; output-device failure must not lose input.
    try {
      if (!audioContext) audioContext = new AudioContext();
      audioContext.resume().catch(() => {
        if (isCurrent()) $("#duplexState").textContent = "声音未就绪，回答文字仍会显示";
      });
    } catch (_) {
      $("#duplexState").textContent = "声音未就绪，回答文字仍会显示";
    }
    const connection = connectRealtime({media: false});
    const generation = connectionGeneration;
    await connection;
    if (!isCurrent()) return;
    if (generation !== connectionGeneration) throw Error("连接已取消，输入已保留，可重新发送");
    if (!socket || socket.readyState !== WebSocket.OPEN) throw Error("实时会话尚未就绪");
    const pending = {type: "control", transcript, clientRequestId: crypto.randomUUID()};
    currentTurnId = pending.clientRequestId;
    localSubmittedTurns.set(currentTurnId, {
      turnId: currentTurnId, text: transcript, sequence: Number.MAX_SAFE_INTEGER,
      status: "pending-send", delivery: {status: "pending-send"}, source: "explicit-control",
    });
    renderWorkCenter();
    if (input.value === draft) input.value = "";
    // Reuse enabled sensors. Send the fresh frame before the question so the
    // server associates it with this turn, just as it does for spoken input.
    try { sendVideoFrame(); }
    catch (_) { $("#cameraState").textContent = "当前帧采集失败，检查摄像头"; }
    pending.capture = {clockId: mediaCaptureClockId, monotonicMs: performance.now()};
    turnOutbox().add(pending);
    socket.send(JSON.stringify(pending));
    $("#textTurnError").textContent = "已发送，等待助手接收…";
  } catch (error) { if (isCurrent()) $("#textTurnError").textContent = error.message; }
  finally { if (isCurrent()) button.disabled = false; }
};

$("#textTurnInput").addEventListener("keydown", event => {
  if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  if (!event.repeat && !$("#textTurnSend").disabled) $("#textTurnForm").requestSubmit();
});

$("#turnWorkHistory").onclick = $("#turnHistory").onclick = async event => {
  const inspectButton = event.target.closest("[data-inspect-turn]");
  if (inspectButton) {
    inspectedTurnId = inspectButton.dataset.inspectTurn;
    renderTurnWorkflow();
    location.hash = "/audit";
    showWorkspaceView();
    $("#turnWorkflowDetails").open = true;
    $("#turnWorkflowDetails").scrollIntoView({block: "nearest"});
    $("#turnWorkflowDetails > summary").focus({preventScroll: true});
    return;
  }
  const action = event.target.closest("[data-answer-action]");
  if (action) {
    action.disabled = true;
    try {
      const result = await post("/api/answers/control", {sessionId: selectedSessionId,
        turnId: action.dataset.answerTurn, action: action.dataset.answerAction});
      render(result.state);
    } catch (error) { $("#textTurnError").textContent = error.message; }
    finally { action.disabled = false; }
    return;
  }
  const button = event.target.closest("[data-reask-turn]");
  if (!button) return;
  const turn = state?.workLedger?.turnInbox?.find(item => item.turnId === button.dataset.reaskTurn);
  if (turn) {
    $("#textTurnInput").value = turn.text;
    $("#textTurnInput").focus();
  }
};

async function switchRuntime(button, path, payload) {
  if (sessionSwitchBusy) return;
  const isCurrent = sessionScope(), sid = selectedSessionId, media = Boolean(stream);
  const wasRealtime = Boolean(socket || connectingPromise);
  if (wasRealtime) disconnectRealtime();
  if (!isCurrent()) return;
  button.disabled = true;
  try {
    const result = await post(path, {...payload, sessionId: sid});
    if (!isCurrent()) return;
    render(result);
    if (wasRealtime) await connectRealtime({media});
    return isCurrent();
  } catch (error) { alert(error.message); }
  finally { button.disabled = false; }
}

$("#realtimeButton").onclick = async () => { try { socket || connectingPromise ? disconnectRealtime() : await connectRealtime(); } catch (error) { alert("启动失败：" + error.message); disconnectRealtime(); } };
$("#cameraButton").onclick = async () => { try { stream || mediaPromise ? stopMedia() : await ensureMedia(); } catch (error) { alert("设备访问失败：" + error.message); } };
$("#videoFps").onchange = () => { if (!socket) return; clearInterval(videoTimer); videoTimer = setInterval(sendVideoFrame, 1000 / Number($("#videoFps").value)); };
$("#mirrorCamera").checked = mirrorCamera;
$("#microphoneEnabled").checked = microphoneEnabled;
$("#microphoneEnabled").onchange = event => {
  microphoneEnabled = event.currentTarget.checked;
  localStorage.setItem("egoassist.microphone.enabled", String(microphoneEnabled));
  stream?.getAudioTracks().forEach(track => { track.enabled = microphoneEnabled; });
  if (stream) $("#cameraState").textContent = microphoneEnabled ? "摄像头与麦克风在线" : "摄像头在线 · 麦克风已关闭";
};
applyCameraOrientation();
$("#mirrorCamera").onchange = event => {
  mirrorCamera = event.currentTarget.checked;
  localStorage.setItem("egoassist.camera.mirror", String(mirrorCamera));
  applyCameraOrientation();
  if (socket?.readyState === WebSocket.OPEN) sendMediaConfiguration(socket);
};
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
    TurnOutbox.clearUser(sessionStorage, currentUser.id);
    showLogin();
  } catch (error) { alert(error.message); }
};
$("#newTask").onclick = () => $("#taskDialog").showModal();
$("#cancelTask").onclick = () => $("#taskDialog").close();
$("#taskDialog").onclose = () => $("#taskForm").reset();
$("#taskForm").onsubmit = async event => {
  event.preventDefault();
  const title = $("#customTitle").value.trim();
  const steps = $("#customSteps").value.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  if (await switchRuntime($("#createTask"), "/api/tasks/create", {title, steps})) $("#taskDialog").close();
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
async function changeSession(targetId, label = null) {
  if (sessionSwitchBusy || !currentUser) return;
  sessionSwitchBusy = true;
  const uid = currentUser.id, revision = ++sessionRevision;
  const isCurrent = () => currentUser?.id === uid && sessionRevision === revision;
  sessionDrafts.set(selectedSessionId, $("#textTurnInput").value);
  $("#sessionSelect").disabled = $("#newSession").disabled = $("#textTurnSend").disabled = true;
  const wasRealtime = Boolean(socket || connectingPromise);
  const media = Boolean(stream);
  disconnectRealtime();
  $("#textTurnError").textContent = "正在切换案例，未发送输入已保留在原案例…";
  try {
    if (!isCurrent()) return;
    if (label !== null) {
      const created = await post("/api/sessions", {label});
      if (!isCurrent()) return;
      targetId = created.session.id;
    }
    selectedSessionId = targetId;
    window.resetConversationHistory?.();
    toolActivityView.reset(targetId);
    state = null;
    localSubmittedTurns.clear();
    $("#textTurnInput").value = sessionDrafts.get(targetId) || "";
    $("#turnHistory").replaceChildren();
    $("#transcript").textContent = "";
    $("#assistantCaption").replaceChildren();
    localStorage.setItem("egoassist.session." + uid, targetId);
    await refreshSessionList(targetId);
    if (!isCurrent()) return;
    render(await request(selectedStatePath()));
    if (!isCurrent()) return;
    $("#textTurnError").textContent = "";
    if (wasRealtime) await connectRealtime({media});
  } catch (error) {
    if (isCurrent()) $("#textTurnError").textContent = "案例切换未完成 · " + error.message;
  } finally {
    sessionSwitchBusy = false;
    $("#sessionSelect").disabled = $("#newSession").disabled = false;
    if (isCurrent()) $("#textTurnSend").disabled = false;
  }
}

$("#newSession").onclick = async () => {
  const label = prompt("实验案例名称", "新实验案例");
  if (label === null || !label.trim()) return;
  await changeSession(null, label.trim());
};
$("#sessionSelect").onchange = event => changeSession(event.currentTarget.value);
function renderInsight(result) {
  const kind = {daily: "日报", weekly: "周报", monthly: "月报", suggestions: "当前建议"};
  $("#insightResultTitle").textContent = [kind[result.period?.kind] || "报告", result.period?.anchor || ""].filter(Boolean).join(" · ");
  const coverage = result.coverage || {};
  $("#insightStatus").textContent = [result.status, result.cached ? "复用已生成结果" : result.latencyMs != null ? result.latencyMs + " ms" : "",
    "记录 " + (coverage.includedRecords || 0) + "/" + (coverage.totalRecords || 0),
    coverage.complete === false ? "证据不完整" : ""].filter(Boolean).join(" · ");
  const sources = new Map((result.sources || []).map(item => [item.sourceId, item]));
  $("#insightResults").innerHTML = ["summary", "suggestions"].map(key => {
    const items = result[key] || [];
    return "<h3>" + (key === "summary" ? "记录摘要" : "建议") + "</h3>" + (items.length ? items.map(item =>
      "<article class='insight-item'><p>" + esc(item.text) + "</p><details><summary>来源 " + item.sourceIds.length +
      "</summary>" + item.sourceIds.map(id => { const source = sources.get(id); return "<div class='insight-source'><small>" +
        esc(id + " · " + (source?.recordedLocalAt || source?.recordedAt || "")) + "</small><p>" + esc(source?.text || "来源不可用") + "</p></div>"; }).join("") + "</details></article>"
    ).join("") : "<p>" + (key === "summary" ? "暂无摘要" : "暂无有依据的建议") + "</p>");
  }).join("") + "<details><summary>生成审计</summary><pre>" + esc(JSON.stringify(result, null, 2)) + "</pre></details>";
}

async function syncInsightSession() {
  const sid = selectedSessionId;
  const isCurrent = sessionScope();
  if (!currentUser || !sid || insightSession === sid) return;
  insightSession = sid;
  insightTurn = null;
  insightJobs = [];
  selectedInsightJob = null;
  displayedInsightResult = null;
  const selection = ++insightResultRequest;
  $("#insightJobs").replaceChildren();
  promptAuditSignature = "";
  $("#proactiveSuggestions").checked = false;
  $("#insightResults").replaceChildren();
  $("#insightResultTitle").textContent = "尚未选择报告";
  $("#insightStatus").textContent = "";
  $("#actualPromptRequests").replaceChildren();
  const local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
  $("#insightDate").value = local.toISOString().slice(0, 10);
  try {
    const data = await request("/api/insights?session=" + encodeURIComponent(sid));
    if (!isCurrent()) return;
    $("#proactiveSuggestions").checked = data.proactiveEnabled;
    renderInsightJobs(data.jobs || []);
    if (data.results.length && selection === insightResultRequest) {
      const result = data.results.at(-1);
      displayedInsightResult = result.id;
      selectedInsightJob = insightJobs.find(job => job.resultEventId === result.id)?.jobId || null;
      renderInsight(result);
    }
  } catch (error) {
    if (sid === selectedSessionId) {
      insightSession = null;
      $("#insightStatus").textContent = "任务状态暂不可用 · " + error.message;
    }
  }
}

async function generateInsight(automatic = false) {
  if (insightBusy || !currentUser || !selectedSessionId) return;
  const sid = selectedSessionId;
  const isCurrent = sessionScope();
  insightBusy = true;
  insightLastAttempt = Date.now();
  $("#insightGenerate").disabled = true;
  $("#insightStatus").textContent = "正在分析记录";
  try {
    const result = await post("/api/insights", {sessionId: sid, automatic,
      kind: automatic ? "suggestions" : $("#insightKind").value,
      anchor: automatic ? null : $("#insightDate").value,
      scope: automatic ? "case" : $("#insightScope").value});
    if (isCurrent()) {
      if (!automatic || !selectedInsightJob) {
        selectedInsightJob = result.job.jobId;
        insightResultRequest += 1;
      }
      $("#insightStatus").textContent = "已受理 · " + result.acceptedMs + " ms";
      renderInsightJobs([...insightJobs.filter(job => job.jobId !== result.job.jobId), result.job]);
      refreshInsightJobs();
    }
  } catch (error) { if (isCurrent()) $("#insightStatus").textContent = error.message; }
  finally { insightBusy = false; $("#insightGenerate").disabled = false; }
}

function maybeSuggest() {
  if (!currentUser || !state || !$("#proactiveSuggestions").checked || insightBusy || Date.now() - insightLastAttempt < 30000) return;
  if (insightJobs.some(job => ["queued", "running"].includes(job.status) && job.specification.kind === "suggestions")) return;
  const turn = [...(state.events || [])].reverse().find(item => item.type === "message" && item.payload?.message?.role === "user");
  if (!turn || turn.eventId === insightTurn || state.workLedger?.turnInbox?.some(t =>
      ["received", "classifying", "ready", "processing"].includes(t.status) || t.answer?.status === "foreground")) return;
  insightTurn = turn.eventId;
  generateInsight(true);
}

function renderInsightJobs(jobs) {
  insightJobs = jobs;
  const labels = {queued: "排队中", running: "生成中", completed: "已完成", empty: "无记录", failed: "失败", cancelled: "已取消"};
  const kinds = {daily: "日报", weekly: "周报", monthly: "月报", suggestions: "建议"};
  const visible = [...jobs];
  $("#insightJobs").innerHTML = visible.reverse().map(job => "<div class='insight-job' data-insight-job='" + esc(job.jobId) + "'>" +
    "<span>" + esc(kinds[job.specification.kind] + " · " + job.specification.anchor) + "</span><b>" + esc(labels[job.status] || job.status) +
    "</b><small>" + esc(!["queued", "running"].includes(job.status)
      ? (job.durationMs != null ? job.durationMs.toFixed(0) + " ms" : labels[job.status] || job.status)
      : "已等待 " + Math.max(0, (Date.now() - Date.parse(job.createdAt)) / 1000).toFixed(1) + " s") + "</small>" +
    (["queued", "running"].includes(job.status) ? "<small>" + esc(insightPhases[job.phase] || "处理中") +
      (job.model ? " · " + esc(job.model) : "") +
      (job.includedRecords != null ? " · 记录 " + job.includedRecords + "/" + job.totalRecords : "") +
      (job.phase === "waiting-provider" ? " · 单次请求上限 " + job.providerTimeoutSeconds + " s" : "") +
      " · 执行次数 " + job.attempt + "</small>" : "") +
    (job.reason === "recovered" ? "<small>已从服务重启恢复</small>" : "") +
    (job.resultEventId ? "<button data-insight-result='" + esc(job.resultEventId) + "'>查看</button>" : "") +
    (["queued", "running"].includes(job.status) ? "<button data-insight-cancel='" + esc(job.jobId) + "'>取消</button>" : "") +
    (["failed", "cancelled"].includes(job.status) ? "<button data-insight-retry='" + esc(job.jobId) + "'>重试</button>" : "") +
    (job.error ? "<p>" + esc(job.error) + "</p>" : "") + "</div>").join("");
}

async function loadInsightResult(id) {
  const sid = selectedSessionId;
  const isCurrent = sessionScope(), selection = ++insightResultRequest;
  const data = await request("/api/insights?session=" + encodeURIComponent(sid) + "&result=" + encodeURIComponent(id));
  if (isCurrent() && selection === insightResultRequest && data.results.length) {
    displayedInsightResult = id;
    renderInsight(data.results[0]);
  }
}

async function refreshInsightJobs() {
  if (insightPolling || !currentUser || !selectedSessionId) return;
  const sid = selectedSessionId;
  const isCurrent = sessionScope(), selection = insightResultRequest;
  insightPolling = true;
  insightLastPoll = Date.now();
  try {
    const data = await request("/api/insights?session=" + encodeURIComponent(sid) + "&includeResults=false", {signal: AbortSignal.timeout(8000)});
    if (!isCurrent()) return;
    renderInsightJobs(data.jobs || []);
    const job = insightJobs.find(item => item.jobId === selectedInsightJob) || insightJobs.at(-1);
    if (job && ["queued", "running"].includes(job.status)) {
      $("#insightStatus").textContent = (job.status === "queued" ? "排队中" : "生成中") +
        " · " + (insightPhases[job.phase] || "处理中") + " · 状态更新于 " + new Date().toLocaleTimeString();
    }
    if (selection === insightResultRequest && job?.resultEventId && job.resultEventId !== displayedInsightResult) await loadInsightResult(job.resultEventId);
  } catch (error) { if (sid === selectedSessionId) $("#insightStatus").textContent = "连接中断，显示上次状态 · " + error.message; }
  finally { insightPolling = false; }
}

$("#insightJobs").onclick = async event => {
  const button = event.target.closest("button");
  if (!button) return;
  button.disabled = true;
  try {
    if (button.dataset.insightResult) {
      selectedInsightJob = insightJobs.find(job => job.resultEventId === button.dataset.insightResult)?.jobId;
      await loadInsightResult(button.dataset.insightResult);
    } else if (button.dataset.insightCancel) {
      await post("/api/insights/cancel", {sessionId: selectedSessionId, jobId: button.dataset.insightCancel});
      await refreshInsightJobs();
    } else if (button.dataset.insightRetry) {
      const job = insightJobs.find(item => item.jobId === button.dataset.insightRetry);
      $("#insightKind").value = job.specification.kind;
      $("#insightDate").value = job.specification.anchor;
      $("#insightScope").value = job.specification.scope;
      await generateInsight();
    }
  } catch (error) { $("#insightStatus").textContent = error.message; }
  finally { button.disabled = false; }
};
setInterval(() => {
  if (insightJobs.some(job => ["queued", "running"].includes(job.status)) || Date.now() - insightLastPoll > 5000) refreshInsightJobs();
}, 1200);

async function refreshPromptAudit(older = false) {
  older = older === true;
  const sid = selectedSessionId;
  if (!currentUser || !sid || promptAuditPolling) return;
  promptAuditPolling = true;
  try {
    if (promptAuditSession !== sid) {
      promptAuditSession = sid; promptAuditCursor = null; promptAuditRows.clear();
    }
    const data = await request("/api/prompts?session=" + encodeURIComponent(sid) +
      (older && promptAuditCursor ? "&before=" + encodeURIComponent(promptAuditCursor) : ""));
    if (sid !== selectedSessionId || !currentUser) return;
    const first = promptAuditRows.size === 0;
    const overlaps = data.requests.some(item => promptAuditRows.has(item.id));
    for (const item of data.requests) promptAuditRows.set(item.id, item);
    if (older || first || !overlaps) promptAuditCursor = data.nextBefore;
    const container = $("#actualPromptRequests");
    const open = new Set([...container.querySelectorAll("details[open]")].map(el => el.dataset.id));
    container.innerHTML = [...promptAuditRows.values()].sort((a,b) => b.timestamp.localeCompare(a.timestamp)).map(item =>
      '<details data-id="' + esc(item.id) + '"' + (open.has(item.id) ? " open" : "") + "><summary>" +
      esc((item.eventType || item.type) + " · " + item.timestamp + " · " + item.id) + "</summary><pre>" +
      esc(JSON.stringify(item, null, 2)) + "</pre></details>").join("") || "暂无已发送请求";
    $("#promptAuditCount").textContent = "已加载 " + promptAuditRows.size + " / " + data.totalAuditEvents + " 条";
    $("#olderPromptRequests").hidden = !promptAuditCursor;
    $("#modelConfiguration").textContent = JSON.stringify(data.configuration, null, 2);
    const statuses = {queued:"等待后台记忆解析", running:"正在解析原始记录", failed:"解析失败，原话已保留", completed:"解析完成"};
    $("#memoryRecoveryJobs").innerHTML = (data.memoryRecovery || []).map(job =>
      '<p>' + esc((statuses[job.status] || job.status) + " · " + job.text + " · 尝试 " + job.attempt +
      (job.error ? " · " + job.error : "") + (job.cardIds ? " · 保存 " + job.cardIds.length + " 条" : "")) +
      (job.status === "failed" ? '<button data-retry-memory="' + esc(job.turnId) + '">重试解析</button>' : "") + "</p>").join("");
    const signature = JSON.stringify(data.templates.map(item => item.sha256));
    if (signature !== promptAuditSignature) {
      promptAuditSignature = signature;
      $("#promptTemplates").innerHTML = data.templates.map(item => "<details><summary>" + esc(item.path) +
        "</summary><small>SHA256 " + esc(item.sha256) + "</small><pre>" + esc(JSON.stringify(item.templates, null, 2)) + "</pre></details>").join("");
    }
  } catch (error) { if (sid === selectedSessionId) $("#actualPromptRequests").textContent = error.message; }
  finally { promptAuditPolling = false; }
}

$("#insightForm").onsubmit = event => { event.preventDefault(); generateInsight(); };
$("#proactiveSuggestions").onchange = async event => {
  const enabled = event.target.checked;
  try { await post("/api/insights/policy", {sessionId: selectedSessionId, enabled}); insightTurn = null; maybeSuggest(); }
  catch (error) { event.target.checked = !enabled; $("#insightStatus").textContent = error.message; }
};
$("#refreshPromptAudit").onclick = refreshPromptAudit;
$("#olderPromptRequests").onclick = () => refreshPromptAudit(true);
$("#memoryRecoveryJobs").onclick = async event => {
  const button = event.target.closest("[data-retry-memory]");
  if (!button) return;
  button.disabled = true;
  try { await post("/api/memory/recovery/retry", {sessionId:selectedSessionId, turnId:button.dataset.retryMemory}); await refreshPromptAudit(); }
  catch (error) { button.disabled = false; $("#promptAuditCount").textContent = error.message; }
};
$("#promptAuditDetails").ontoggle = refreshPromptAudit;
$("#promptTemplatesDetails").ontoggle = refreshPromptAudit;
setInterval(() => { if (location.hash === "#/audit" && ($("#promptAuditDetails").open || $("#promptTemplatesDetails").open)) refreshPromptAudit(); maybeSuggest(); }, 3000);

boot().catch(error => { showLogin(); $("#authError").textContent = error.message; });
async function refreshConsoleState() {
  if (socket?.readyState === WebSocket.OPEN) {
    if (stateRefreshPendingAt) {
      if (Date.now() - stateRefreshPendingAt <= 8000) return;
      $("#stateConnectionStatus").textContent = "实时状态更新超过 8 秒，正在查询服务器状态。";
    } else {
      if (socket.bufferedAmount > VIDEO_BUFFER_HIGH_WATER) return;
      stateRefreshPendingAt = Date.now();
      socket.send(JSON.stringify({type: "refresh"}));
      return;
    }
  } else {
    stateRefreshPendingAt = 0;
  }
  if (!currentUser || !selectedSessionId || detachedStatePolling || Date.now() - lastDetachedStatePoll < 3000) return;
  const isCurrent = sessionScope(), revision = stateSyncRevision;
  detachedStatePolling = true;
  lastDetachedStatePoll = Date.now();
  try {
    const latest = await request(selectedStatePath(), {signal: AbortSignal.timeout(8000)});
    if (isCurrent()) {
      render(latest);
      stateRefreshPendingAt = 0;
    }
  } catch (error) {
    if (isCurrent() && revision === stateSyncRevision)
      $("#stateConnectionStatus").textContent = "状态暂未同步，正在自动重连；当前显示上次快照。";
  }
  finally { detachedStatePolling = false; }
}
setInterval(refreshConsoleState, 1200);
addEventListener("online", () => { lastDetachedStatePoll = 0; refreshConsoleState(); });
addEventListener("beforeunload", () => { disconnectRealtime(); stopMedia(); });
