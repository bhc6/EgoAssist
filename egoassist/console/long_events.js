function eventEvidenceHTML(sources) {
  return sources.map(s => '<p><b>' + esc(s.kind === 'user-statement' ? '用户原话' : '画面观察') + '</b> · ' + esc(new Date(s.at).toLocaleString()) + '</p><blockquote>' +
    esc(s.text) + (s.textTruncated ? '（摘录）' : '') + '</blockquote>' +
    (s.media?.assetId ? '<a target="_blank" rel="noopener" href="' + sceneAsset(s.media) + '">查看原始画面</a>' : '')).join('');
}
function renderLongEvents(value) {
  const root = document.getElementById('longEvents');
  if (!root) return;
  const status = {ongoing:'进行中（据最近证据）', paused:'已暂停', completed:'已结束（据引用证据）', uncertain:'进展待确认'};
  const job = {running:'正在整理', failed:'整理未完成，可重试', queued:'等待恢复', completed:'已整理'};
  const events = value?.events || [];
  const html = '<p>' + esc(job[value?.job?.status] || '等待连续观察证据') + ' · 待整理 ' + (value?.pendingSources || 0) + ' 条</p>' +
    '<p>记录有来源的阶段变化。采样间隔内的行为未知；证据跨度不等于持续活动时长。</p>' +
    events.map(e => '<article><h3>' + esc(e.title) + '</h3><p>' + esc(status[e.status]) +
      (e.observationStale && e.status !== 'completed' ? ' · 暂无近期证据，不能确认仍在进行' : '') + '</p><p>' + esc(e.summary) + '</p>' +
      '<small>证据跨度：' + esc(new Date(e.coverage.firstEvidenceAt).toLocaleString()) + ' — ' + esc(new Date(e.coverage.lastEvidenceAt).toLocaleString()) +
      ' · ' + e.coverage.sampleCount + ' 次画面观察 / ' + e.coverage.statementCount + ' 条自述</small>' +
      '<ol>' + e.phases.map(p => '<li>' + esc(new Date(p.at).toLocaleTimeString()) + ' · ' + esc(p.description) + '</li>').join('') + '</ol>' +
      (e.coverage.gapCount ? '<p>存在 ' + e.coverage.gapCount + ' 段较长证据间隔，期间进展未知。</p>' : '') +
      (e.totalPhases > e.phases.length ? '<p>展示最近 ' + e.phases.length + ' 个阶段，共 ' + e.totalPhases + ' 个。</p>' : '') +
      (e.uncertainties.length ? '<p>待确认：' + esc(e.uncertainties.join('；')) + '</p>' : '') +
      '<details data-disclosure="event-' + esc(e.eventId) + '"><summary>查看原始依据（共 ' + e.totalSources + ' 条）</summary>' +
      '<div class="event-sources">' + eventEvidenceHTML(e.sources) + '</div><button type="button" data-event-evidence="' + esc(e.eventId) + '" data-offset="0">从头查看全部依据</button></details></article>').join('') +
    (!events.length ? '<p>尚未形成有依据的事件。开启环境观察后会自动整理，也可以整理本会话已有记录。</p>' : '') +
    (value?.truncated ? '<p>当前展示最近 ' + events.length + ' 个事件，其余仍保存在记录中。</p>' : '');
  updateDisclosureHTML(root, html, selectedSessionId);
}
document.getElementById('longEvents').addEventListener('click', async event => {
  const button = event.target.closest('[data-event-evidence]');
  if (!button) return;
  button.disabled = true;
  const session = selectedSessionId;
  try {
    const response = await fetch('/api/scene/events/evidence?session=' + encodeURIComponent(session) +
      '&eventId=' + encodeURIComponent(button.dataset.eventEvidence) + '&offset=' + button.dataset.offset);
    if (!response.ok) throw new Error('证据暂不可读取');
    const page = await response.json();
    if (selectedSessionId !== session || !button.isConnected) return;
    const content = button.parentElement.querySelector('.event-sources');
    if (button.dataset.offset === '0') content.innerHTML = '';
    content.insertAdjacentHTML('beforeend', eventEvidenceHTML(page.sources));
    if (page.nextOffset === null) button.remove();
    else { button.dataset.offset = String(page.nextOffset); button.textContent = '继续查看'; button.disabled = false; }
  } catch (error) { button.textContent = error.message + '，点击重试'; button.disabled = false; }
});
document.getElementById('longEventActions').addEventListener('click', async event => {
  const button = event.target.closest('[data-event-action]');
  if (!button) return;
  button.disabled = true;
  try {
    const result = await post('/api/scene/events', {sessionId: selectedSessionId, action: button.dataset.eventAction});
    render(result.state);
    document.getElementById('longEventStatus').textContent = '已安排整理，原始证据保留。';
  } catch (error) { document.getElementById('longEventStatus').textContent = error.message; }
  finally { button.disabled = false; }
});
