/* Frame-local people and acoustic speaker profiles remain separate until confirmed. */
let sharedSceneSignature = null;
let sharedUtteranceSession = null;
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
  document.querySelector("#sharedSpeakers").innerHTML = speakers.map(s => "<p><b>" + esc(s.name || s.speakerId) +
    "</b> · " + (s.wearer ? "已确认佩戴者" : s.name ? "已确认姓名" : "姓名未知") +
    (s.personRef ? " · 已关联参考画面" : " · 画面人物未关联") + "</p>").join("") || "<p>尚无可区分的声音</p>";
  const select = document.querySelector("#speakerIdentitySelect"), previous = select.value;
  select.innerHTML = speakers.map(s => "<option value='" + esc(s.speakerId) + "'>" + esc(s.name || s.speakerId) + "</option>").join("");
  if (speakers.some(s => s.speakerId === previous)) select.value = previous;
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
    const name = speaker.status === "account-input" ? "你的文字" : speaker.name || speaker.speakerId ||
      (speaker.status === "pending" ? "正在分析说话人" : "多人或未知说话人");
    const content = "<b>" + esc(name) + "</b><small>" + esc(row.turnId + (speaker.latencyMs != null ? " · " + speaker.latencyMs + " ms" : "")) +
      "</small><p>" + esc(original?.text || "") + "</p>" +
      (row.utterances || []).map(s => "<p>" + esc(s.name || s.speakerId || "声音未确认") + " · " +
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
