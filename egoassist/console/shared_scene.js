/* Frame-local people and acoustic speaker profiles remain separate until confirmed. */
let sharedSceneSignature = null;
let sharedUtteranceSession = null;
function speakerDisplayName(speaker, profiles = state?.sharedScene?.speakers || []) {
  const profile = profiles.find(p => p.speakerId === speaker?.speakerId || p.memberIds?.includes(speaker?.speakerId));
  return profile?.displayName || profile?.name || speaker?.name ||
    (speaker?.status === "pending" ? "正在分析声音" :
      speaker?.status === "unavailable" ? "声音分析暂不可用" : "声音未确认");
}
function sceneAsset(ref) {
  return "/api/scene/media?session=" + encodeURIComponent(selectedSessionId) + "&asset=" + encodeURIComponent(ref.assetId);
}
function renderSharedScene(scene) {
  const status = document.querySelector("#sharedSceneStatus");
  if (!status) return;
  status.textContent = scene ? (scene.camera === "receiving" ? "正在接收画面" : "暂无新鲜画面") +
    " · " + (scene.speakerModel === "available" ? "本地说话人模型就绪" : "说话人模型不可用") : "等待音视频";
  const signature = JSON.stringify([selectedSessionId, scene]);
  if (signature === sharedSceneSignature) return;
  sharedSceneSignature = signature;
  const vision = scene?.vision;
  document.querySelector("#sharedSceneVisual").innerHTML = vision?.frame ?
    "<img class='shared-scene-image' src='" + sceneAsset(vision.frame) + "' alt='模型观察的现场帧'>" +
    "<p>" + esc(vision.description || "视觉处理失败：" + (vision.error || "未知原因")) + "</p><small>" +
    esc(vision.frame.receivedAt + " · " + vision.latencyMs + " ms · " + vision.frame.frameId) + "</small>" : "<p>等待现场帧</p>";
  const speakers = scene?.speakers || [];
  const speakerPanel = document.querySelector("#sharedSpeakers");
  const speakerSignature = JSON.stringify([selectedSessionId, speakers, scene?.speakerMerges]);
  if (speakerPanel.dataset.signature !== speakerSignature) {
  speakerPanel.dataset.signature = speakerSignature;
  speakerPanel.innerHTML = (speakers.length ? "<p>" + speakers.length + " 个声音档案 · 不代表现场人数，同一个人可能有多个档案。</p>" : "") + speakers.map(s => "<p><b>" + esc(speakerDisplayName(s, speakers)) +
    "</b> · " + (s.wearer ? "已标记为我" : s.name ? "已命名" : "待确认") + " · " + (s.turnCount || 0) + " 段对话" +
    (s.personRef ? " · 已关联参考画面" : " · 画面人物未关联") +
    (s.matchingEligible === false ? " · 混合语音样本，暂不用于身份匹配" : "") +
    (s.memberIds?.length > 1 ? " · 已归并 " + s.memberIds.length + " 个档案" : "") + "</p>" +
    "<details><summary>试听原始片段</summary>" + (s.samples || []).map(sample => "<p>" + esc(new Date(sample.timestamp).toLocaleString()) +
    "</p><audio controls preload='none' src='" + sceneAsset(sample.audio) + "'></audio>").join("") + "</details>").join("") || "<p>尚无可区分的声音</p>";
  const suggestions = scene?.speakerMerges?.suggestions || [];
  speakerPanel.insertAdjacentHTML("beforeend", suggestions.map(s => "<p>疑似重复：" +
    esc(speakerDisplayName({speakerId:s.sourceId}, speakers)) + " / " + esc(speakerDisplayName({speakerId:s.targetId}, speakers)) +
    "。请试听后确认是否同一人。</p>").join(""));
  const merge = scene?.speakerMerges?.latestMerge;
  if (merge) speakerPanel.insertAdjacentHTML("beforeend", "<button type='button' data-undo-speaker-merge='" + esc(merge.id) + "'>撤销最近一次合并</button>");
  }
  const select = document.querySelector("#speakerIdentitySelect"), previous = select.value;
  select.innerHTML = speakers.map(s => "<option value='" + esc(s.speakerId) + "'>" + esc(speakerDisplayName(s, speakers)) + "</option>").join("");
  if (speakers.some(s => s.speakerId === previous)) select.value = previous;
  for (const id of ['speakerMergeSource', 'speakerMergeTarget']) {
    const element = document.getElementById(id);
    if (!element) continue;
    const saved = element.value;
    element.innerHTML = "<option value=''>请选择</option>" + speakers.map(s => "<option value='" + esc(s.speakerId) + "'>" + esc(speakerDisplayName(s, speakers)) + "</option>").join('');
    if (speakers.some(s => s.speakerId === saved)) element.value = saved;
  }
  renderLongEvents(scene?.longEvents);
  const people = document.querySelector("#speakerPersonRef"), person = people.value;
  const selectedPerson = people.selectedOptions[0];
  const preservedOption = selectedPerson?.value ? selectedPerson.outerHTML : "";
  people.innerHTML = "<option value=''>未关联</option>" + (vision?.people || []).map(p =>
    "<option value='" + esc(p.personRef) + "'>" + esc(p.position + " · " + p.description) + "</option>").join("");
  if ((vision?.people || []).some(p => p.personRef === person)) people.value = person;
  else if (person) { people.insertAdjacentHTML("beforeend", preservedOption); people.value = person; }
  const utteranceRoot = document.querySelector("#sharedUtterances");
  if (sharedUtteranceSession !== selectedSessionId) {
    utteranceRoot.replaceChildren();
    sharedUtteranceSession = selectedSessionId;
  }
  const rows = scene?.utterances || [];
  for (const child of [...utteranceRoot.children]) {
    if (!rows.some(row => row.turnId === child.dataset.turnId)) child.remove();
  }
  rows.forEach(row => {
    const speaker = row.speaker || {}, original = state?.workLedger?.turnInbox?.find(t => t.turnId === row.turnId);
    const name = speaker.status === "account-input" ? "你的文字" : speakerDisplayName(speaker, speakers);
    const content = "<b>" + esc(name) + "</b><small>" + esc(row.turnId + (speaker.latencyMs != null ? " · " + speaker.latencyMs + " ms" : "")) +
      "</small><p>" + esc(original?.text || "") + "</p>" +
      (row.utterances || []).map(s => "<p>" + esc(speakerDisplayName(s, speakers)) + " · " +
        esc(s.startMs + "–" + s.endMs + " ms") + "：" + esc(s.text || (s.status === "overlapping-speech" ? "同时说话，无法可靠分配逐字内容" : "转写未完成")) + "</p>").join("");
    let article = [...utteranceRoot.children].find(child => child.dataset.turnId === row.turnId);
    if (!article) {
      article = document.createElement("article"); article.dataset.turnId = row.turnId;
      article.innerHTML = "<div class='utterance-text'></div>" +
        (row.audio ? "<audio controls preload='none' src='" + sceneAsset(row.audio) + "'></audio>" : "") +
        "<details><summary>归属证据</summary><pre></pre></details>";
      utteranceRoot.prepend(article);
    }
    if (article.firstChild.innerHTML !== content) article.firstChild.innerHTML = content;
    article.querySelector("pre").textContent = JSON.stringify(speaker, null, 2);
    const window = row.interactionWindow;
    if (window?.frames?.length && !article.querySelector('.interaction-window')) {
      const details = document.createElement('details');
      details.className = 'interaction-window';
      details.innerHTML = '<summary>这句话对应的画面 · ' + window.frames.length + ' 帧</summary>' +
        '<p>提问前约 ' + ((window.coverageMs || 0) / 1000).toFixed(1) + ' 秒的采样画面，按先后排列。未拍到的动作无法确认。</p>' +
        (window.gaps?.length ? '<p>期间有采样间隔，不能视为连续录像。</p>' : '') +
        '<div style="display:flex;gap:8px;overflow:auto">' + window.frames.map(frame =>
          '<figure style="margin:0;flex:0 0 160px"><img style="width:160px" loading="lazy" src="' + sceneAsset(frame) +
          '" alt="提问前的画面"><figcaption>' + Math.abs(frame.offsetMs / 1000).toFixed(2) + ' 秒前</figcaption></figure>').join('') + '</div>';
      article.append(details);
    }
  });
}
document.querySelector("#speakerIdentityForm").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await post("/api/scene/identity", {sessionId: selectedSessionId,
      speakerId: document.querySelector("#speakerIdentitySelect").value,
      name: document.querySelector("#speakerIdentityName").value,
      wearer: document.querySelector("#speakerIsWearer").checked,
      personRef: document.querySelector("#speakerPersonRef").value || null});
    render(result.state);
    document.querySelector("#speakerIdentityStatus").textContent = "身份关联已保存";
  } catch (error) { document.querySelector("#speakerIdentityStatus").textContent = error.message; }
  finally { button.disabled = false; }
});

document.querySelector('#speakerMergeForm').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.submitter;
  button.disabled = true;
  try {
    const result = await post('/api/scene/speakers/merge', {sessionId: selectedSessionId, action: 'merge',
      sourceId: document.querySelector('#speakerMergeSource').value, targetId: document.querySelector('#speakerMergeTarget').value});
    render(result.state);
    document.querySelector('#speakerMergeStatus').textContent = '已合并，可撤销；原始语音保留。';
  } catch (error) { document.querySelector('#speakerMergeStatus').textContent = error.message; }
  finally { button.disabled = false; }
});
document.querySelector('#sharedSpeakers').addEventListener('click', async event => {
  const button = event.target.closest('[data-undo-speaker-merge]');
  if (!button) return;
  button.disabled = true;
  try {
    const result = await post('/api/scene/speakers/merge', {sessionId: selectedSessionId, action: 'undo', mergeId: button.dataset.undoSpeakerMerge});
    render(result.state);
    document.querySelector('#speakerMergeStatus').textContent = '已撤销合并，原称呼与档案已恢复。';
  } catch (error) { document.querySelector('#speakerMergeStatus').textContent = error.message; button.disabled = false; }
});
