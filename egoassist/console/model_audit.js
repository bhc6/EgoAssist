/* Owner-scoped, lazy model input/output inspection. No current-template reconstruction. */
const modelAuditView = (() => {
  let scope = null, generation = 0, busy = false, cursor = null, filter = '', calls = new Map();
  let selected = null, selectedVersion = null, loaded = null, detailGeneration = 0;
  let erasureVersion = null;
  const root = () => document.querySelector('#modelAudit');
  const currentScope = () => currentUser && selectedSessionId ? currentUser.id + ':' + selectedSessionId : null;
  const labels = {recorded:'已记录', partial:'部分记录', missing:'未记录', 'awaiting-output':'等待输出 / 无终态记录',
    completed:'完成', received:'已收到输出', failed:'失败', unavailable:'不可用', cancelled:'已取消', observed:'已观察', transcribed:'已转写'};
  const label = value => labels[value] || value || '未记录';
  const version = call => JSON.stringify([call.updatedAt, call.status, call.inputEventIds, call.outputEventIds]);
  function reset() {
    generation++; detailGeneration++; scope = null; busy = false; cursor = null; filter = '';
    calls.clear(); selected = selectedVersion = loaded = null;
    erasureVersion = null;
    if (root()) {
      root().querySelector('[data-model-latest]').replaceChildren();
      root().querySelector('[data-model-calls]').replaceChildren();
      root().querySelector('[data-model-detail]').replaceChildren();
      root().querySelector('[data-model-filter]').innerHTML = '<option value="">全部模型</option>';
      root().querySelector('[data-model-more]').hidden = true;
      root().querySelector('[data-model-status]').textContent = '';
      for (const node of root().querySelectorAll('[data-model-latest], [data-model-calls]')) {
        node._renderHTML = null; node._renderContext = null;
      }
    }
  }
  function row(call) {
    return '<button type="button" class="model-call" data-model-call="' + esc(call.id) + '" aria-pressed="' + (selected === call.id) + '">' +
      '<b>' + esc(call.model) + '</b><span>' + esc(call.stage + ' · ' + label(call.status)) + '</span><small>' +
      esc(call.timestamp + ' · 输入：' + label(call.inputStatus) + ' · 输出：' + label(call.outputStatus)) + '</small></button>';
  }
  async function refresh(older = false) {
    if (!root()) return;
    const next = currentScope();
    if (scope !== next) { reset(); scope = next; }
    if (!scope || busy) return;
    const epoch = generation, sid = selectedSessionId;
    busy = true;
    try {
      const params = new URLSearchParams({session:sid, modelCalls:'1'});
      if (filter) params.set('model', filter);
      if (older && cursor) params.set('before', cursor);
      const data = await fetchJSON('/api/prompts?' + params);
      if (epoch !== generation || scope !== currentScope()) return;
      if (erasureVersion !== null && erasureVersion !== data.erasureVersion) {
        reset(); scope = currentScope(); await refresh(); return;
      }
      erasureVersion = data.erasureVersion;
      const first = !calls.size, overlaps = data.calls.some(call => calls.has(call.id));
      for (const call of data.calls) calls.set(call.id, call);
      // Refresh loaded older calls when a formerly pending call finishes.
      for (const call of data.models) if (calls.has(call.id)) calls.set(call.id, call);
      if (older || first || !overlaps) cursor = data.nextBefore;
      const options = root().querySelector('[data-model-filter]');
      const optionsHTML = '<option value="">全部模型</option>' + data.models.map(call => '<option value="' + esc(call.model) + '">' + esc(call.model) + '</option>').join('');
      if (options.innerHTML !== optionsHTML) { options.innerHTML = optionsHTML; options.value = filter; }
      updateDisclosureHTML(root().querySelector('[data-model-latest]'), data.models.map(row).join('') || '<p>当前会话尚无模型调用记录。</p>', scope);
      updateDisclosureHTML(root().querySelector('[data-model-calls]'), [...calls.values()].sort((a,b) => b.order - a.order).map(row).join(''), scope + filter);
      root().querySelector('[data-model-more]').hidden = !cursor;
      root().querySelector('[data-model-status]').textContent = '当前会话共 ' + data.totalCalls + ' 次已识别调用；每个模型显示最近一次记录。';
      const selectedCall = calls.get(selected) || data.models.find(call => call.id === selected);
      if (selectedCall && version(selectedCall) !== selectedVersion) await inspect(selectedCall.id, selectedCall);
    } catch (error) {
      if (epoch === generation && scope === currentScope()) root().querySelector('[data-model-status]').textContent = error.message;
    } finally { if (epoch === generation) busy = false; }
  }
  function contentHTML(items) {
    if (!items.length) return '<p>这次调用没有保存此项，不能从当前模板补造。</p>';
    return items.map(item => {
      const media = [];
      const json = JSON.stringify(item.content, (_key, value) => {
        if (typeof value === 'string' && /^data:(image\/(jpeg|png|webp)|audio\/(wav|mpeg|pcm));base64,[A-Za-z0-9+/=]+$/.test(value)) {
          if (!media.includes(value)) media.push(value);
          return '[媒体 ' + media.indexOf(value) + '，' + value.length + ' 字符；完整数据见导出]';
        }
        return value;
      }, 2);
      return '<article><small>日志 ID：' + esc(item.eventId) + '</small><div class="model-media">' + media.slice(0,24).map((value,index) =>
        '<figure><figcaption>媒体 ' + index + '</figcaption>' + (value.startsWith('data:image/') ? '<img loading="lazy" alt="实际请求图片 ' + index + '" src="' + value + '">' :
        '<audio controls preload="none" src="' + value + '"></audio>') + '</figure>').join('') + '</div>' +
        (media.length > 24 ? '<p>更多媒体保留在完整 JSON 导出中。</p>' : '') + '<pre>' + esc(json) + '</pre></article>';
    }).join('');
  }
  async function inspect(id, summary) {
    const epoch = generation, detailEpoch = ++detailGeneration, key = scope, sid = selectedSessionId;
    selected = id; loaded = null;
    const target = root().querySelector('[data-model-detail]');
    target.innerHTML = '<p role="status">正在读取该次调用的实际上下文…</p>';
    try {
      const data = await fetchJSON('/api/prompts?' + new URLSearchParams({session:sid, modelCall:id}));
      if (epoch !== generation || detailEpoch !== detailGeneration || key !== currentScope()) return;
      loaded = data.call; selectedVersion = version(loaded);
      target.innerHTML = '<h3>' + esc(loaded.model + ' · ' + loaded.stage) + '</h3><p>' + esc(label(loaded.status)) +
        (loaded.latencyMs != null ? ' · ' + esc(Math.round(loaded.latencyMs)) + ' ms' : '') + '</p><p>调用 ID：<code>' + esc(loaded.id) + '</code>' +
        (loaded.responseId ? ' · 回答 ID：<code>' + esc(loaded.responseId) + '</code>' : '') + '</p>' +
        (loaded.error ? '<p>错误：' + esc(loaded.error) + '</p>' : '') +
        '<p>输入：' + esc(label(loaded.inputStatus)) + '；输出：' + esc(label(loaded.outputStatus)) +
        '。仅展示应用实际留存的内容；部分记录可能缺少会话历史、媒体或请求参数。</p>' +
        '<button type="button" data-model-export>导出完整调用 JSON</button>' +
        '<details open><summary>实际输入 / 上下文</summary>' + contentHTML(loaded.inputs) + '</details>' +
        '<details open><summary>收到的输出 / 失败记录</summary>' + contentHTML(loaded.outputs) + '</details>';
      root().querySelectorAll('[data-model-call]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.modelCall === id)));
    } catch (error) {
      if (epoch === generation && detailEpoch === detailGeneration && key === currentScope()) target.textContent = error.message;
    }
  }
  function bind() {
    if (!root()) return;
    root().onclick = event => {
      const call = event.target.closest('[data-model-call]');
      if (call) { inspect(call.dataset.modelCall); return; }
      if (event.target.closest('[data-model-refresh]')) refresh();
      if (event.target.closest('[data-model-more]')) refresh(true);
      if (event.target.closest('[data-model-export]') && loaded && scope === currentScope()) {
        const url = URL.createObjectURL(new Blob([JSON.stringify(loaded,null,2)], {type:'application/json'}));
        const link = document.createElement('a'); link.href = url; link.download = 'model-call-' + loaded.id.replace(/[^a-zA-Z0-9_-]/g,'_') + '.json'; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      }
    };
    root().querySelector('[data-model-filter]').onchange = event => {
      filter = event.target.value; generation++; busy = false; calls.clear(); cursor = null; refresh();
    };
    addEventListener('hashchange', () => { if (location.hash === '#/audit') refresh(); });
    setInterval(() => { if (scope !== currentScope()) reset(); if (location.hash === '#/audit' && !document.hidden) refresh(); }, 3000);
  }
  bind();
  return {reset, refresh};
})();
