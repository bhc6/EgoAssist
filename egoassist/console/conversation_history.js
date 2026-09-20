/* Durable dialogue and live captions share stable message IDs, not job status. */
(() => {
  let scope = null, rows = new Map(), live = new Map(), total = 0, cursor = null;
  let busy = false, epoch = 0, wanted = 50;
  const panel = document.querySelector('#turnHistory');
  const older = document.querySelector('#olderConversation');
  const count = document.querySelector('#conversationCount');
  const error = document.querySelector('#conversationError');
  function reset(next) {
    scope = next; epoch++; busy = false; wanted = 50;
    rows = new Map(); live = new Map(); total = 0; cursor = null;
    panel.replaceChildren(); count.textContent = ''; error.textContent = ''; older.hidden = true;
  }
  function draw(prepend = false) {
    const follow = panel.scrollHeight - panel.scrollTop - panel.clientHeight < 48;
    const top = panel.scrollTop, height = panel.scrollHeight;
    const labels = {interrupted: '已中断 · 已生成片段，不代表全部播出', streaming: '正在生成',
      generated: '已生成 · 等待保存记录', received: '已接收 · 等待加载记录', completed: '已保存'};
    const messages = [...rows.values(), ...[...live.values()].filter(row => !rows.has(row.id))];
    const html = messages.map(row => "<div class='turn-history-row' data-message-id='" + esc(row.id) +
      "' data-turn-id='" + esc(row.turnId || '') + "'><small>" +
      esc(row.role === 'assistant' ? (row.source === 'background-text' ? '助手 · 后台补全' : '助手') :
        row.source === 'input-audio-transcript' ? '现场语音' : '你') + ' · ' +
      esc(row.timestamp ? new Date(row.timestamp).toLocaleString() : '') + ' · ' + esc(labels[row.status] || row.status) +
      "</small><p class='" + (row.role === 'assistant' ? 'completed-answer' : 'user-message') + "' style='white-space:pre-wrap;overflow-wrap:anywhere'>" +
      esc(row.text) + '</p>' + (row.turnId && state?.workLedger?.turnInbox?.some(turn => turn.turnId === row.turnId) ? "<button type='button' class='inspect-turn' data-inspect-turn='" + esc(row.turnId) +
        "'>查看处理记录</button>" : '') + '</div>').join('');
    if (panel.innerHTML !== html) {
      panel.innerHTML = html;
      panel.scrollTop = prepend ? top + panel.scrollHeight - height : follow ? panel.scrollHeight : top;
    }
    count.textContent = '已加载 ' + rows.size + ' / ' + total + ' 条已保存消息' + (live.size ? ' · 有实时内容' : '');
    older.hidden = !cursor; older.disabled = busy;
  }
  async function refresh(more = false) {
    const next = currentUser && selectedSessionId ? currentUser.id + '/' + selectedSessionId : null;
    if (scope !== next) reset(next);
    if (!scope || busy) return;
    busy = true; older.disabled = true;
    const generation = epoch, sid = selectedSessionId, isCurrent = sessionScope();
    const target = wanted + (more ? 50 : 0);
    try {
      // Refresh the loaded range so edits/deletions cannot linger in a browser cache.
      let before = null, pages = [], firstTotal = null, required = target;
      do {
        const page = await request('/api/conversation?session=' + encodeURIComponent(sid) + '&limit=200' +
          (before ? '&before=' + encodeURIComponent(before) : ''), {signal: AbortSignal.timeout(8000)});
        if (generation !== epoch || !isCurrent()) return;
        if (firstTotal === null) {
          firstTotal = page.total;
          required += Math.max(0, page.total - total - (rows.size ? 0 : page.total));
        }
        pages = [...page.messages, ...pages]; before = page.nextBefore;
      } while (before && pages.length < required);
      // Keep the requested number without losing the cursor for the rest.
      if (pages.length > required) { pages = pages.slice(-required); before = pages[0].id; }
      rows = new Map(pages.map(row => [row.id, row])); total = firstTotal; cursor = before; wanted = rows.size || 50;
      for (const id of rows.keys()) live.delete(id);
      error.textContent = ''; draw(more);
    } catch (failure) {
      if (generation === epoch && isCurrent()) error.textContent = '对话记录加载失败：' + failure.message;
    } finally {
      if (generation === epoch) { busy = false; older.disabled = false; }
    }
  }
  window.conversationHistoryEvent = message => {
    const next = currentUser && selectedSessionId ? currentUser.id + '/' + selectedSessionId : null;
    if (scope !== next) reset(next);
    if (!scope) return;
    if (message.type === 'user_transcript' && message.turnId) {
      const id = 'user:' + message.turnId;
      live.set(id, {id, role: 'user', turnId: message.turnId, text: message.text, status: 'received',
        source: message.channel === 'explicit_control' ? 'explicit-control' : 'input-audio-transcript', timestamp: new Date().toISOString()});
      draw();
    } else if (message.type === 'assistant_transcript' && message.responseId && ['conversation', 'proactive-utterance'].includes(message.purpose)) {
      const id = 'assistant:' + message.responseId, previous = live.get(id);
      live.set(id, {id, role: 'assistant', turnId: message.origin?.turnId,
        text: message.done ? message.text : (previous?.text || '') + message.text,
        status: message.done ? 'generated' : 'streaming', timestamp: previous?.timestamp || new Date().toISOString()});
      draw();
    } else if (message.type === 'response_status') {
      const row = live.get('assistant:' + message.responseId);
      if (row) { row.status = message.status === 'completed' ? 'generated' : message.status; draw(); }
      void refresh();
    }
  };
  window.resetConversationHistory = () => reset(null);
  older.onclick = () => refresh(true);
  setInterval(() => { if (!document.hidden) void refresh(); }, 2000);
  void refresh();
})();
