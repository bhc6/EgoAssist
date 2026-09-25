/* Read-only projection of runtime snapshots. Never starts a lane or model call. */
window.LaneView = (() => {
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
  const time = value => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleTimeString() : "暂无";
  const statuses = {queued:"排队", running:"处理中", completed:"已完成", failed:"失败", cancelled:"已取消", foreground:"前台处理", paused:"已暂停", stale:"需更新", empty:"暂无记录"};
  const phases = {queued:"等待处理", classifying:"理解请求", ready:"等待按序处理", retrying:"正在重试", "waiting-vision":"核对现场", committing:"检索与处理", generating:"生成回答"};
  const kinds = {"memory.recovery.job":"记忆解析", "long_event.job":"事件整理", "insight.job":"建议 / 报告", daily:"日记", weekly:"周回顾", monthly:"月回顾"};
  let session = null, updated = 0;
  const count = value => Number.isFinite(Number(value)) ? Number(value) : 0;
  function paint(id, html, context) {
    const root = document.getElementById(id);
    if (!root) return;
    const focus = root.contains(document.activeElement) ? document.activeElement.dataset.laneLink : null;
    updateDisclosureHTML(root, html, context);
    if (focus) [...root.querySelectorAll('[data-lane-link]')].find(node => node.dataset.laneLink === focus)?.focus({preventScroll:true});
  }
  function stages(items) {
    return '<ol class="lane-stages">' + items.map(([label, status]) => `<li data-stage="${status}"><span>${escape(label)}</span><small>${{done:"已有结果",active:"处理中",wait:"等待",optional:"按需处理"}[status]}</small></li>`).join('') + '</ol>';
  }
  function jobs(rows) {
    return rows.length ? '<ul class="lane-jobs">' + rows.slice(0,6).map(row => `<li><b>${escape(kinds[row.kind] || "请求")}</b><span>${escape(phases[row.phase] || row.phase || statuses[row.status] || row.status || "等待")}</span>${['daily','weekly','monthly'].includes(row.kind)?`<a data-lane-link="diary-${escape(row.id)}" href="diary.html?entry=${encodeURIComponent(row.id)}" target="_blank" rel="noopener">查看这次整理 →</a>`:`<small>${escape(row.id || row.turnId || "")}</small>`}</li>`).join('') + '</ul>' : '<p class="lane-empty">当前没有排队任务</p>';
  }
  function render(state) {
    const data = state?.runtimeLanes;
    const sameSession = session === state?.sessionId;
    session = state?.sessionId;
    updated = data ? Date.now() : 0;
    const o = data?.observation || {}, a = o.ambient || {}, t = o.task || {};
    const i = data?.interaction || {}, s = i.scheduler || {}, answers = i.answers || {};
    const m = data?.memory || {}, work = m.jobs || {}, diary = m.accountDiary, dj = diary?.jobs || {};
    const prep = i.preparation || [];
    const turns = state?.workLedger?.turnInbox || [];
    const latestTurn = turns[turns.length-1];
    const latestEvents = (state?.events || []).filter(event => event.payload?.turnId === latestTurn?.turnId);
    const readForLatest = latestEvents.some(event => (event.type === 'memory.read' || event.payload?.type === 'memory.read') && event.payload?.status === 'completed');
    const answeredLatest = latestTurn?.answer?.status === 'completed' || latestTurn?.delivery?.status === 'completed';
    const latestAnswer = [...turns].reverse().find(row => row.answer?.status === 'completed' || row.delivery?.status === 'completed');
    const deliveredResponseId = latestAnswer?.answer?.responseId || latestAnswer?.delivery?.responseId;
    const deliveredMessage = [...(state?.events || [])].reverse().find(event => {
      const row = event.payload || event;
      return deliveredResponseId && row.responseId === deliveredResponseId && row.message?.role === 'assistant' && row.status === 'completed';
    });
    const deliveredText = latestAnswer?.answer?.text || deliveredMessage?.payload?.message?.content;
    const observed = state?.ambient?.observation;
    const frameFresh = o.cameraAt && Date.now() - Date.parse(o.cameraAt) <= 10000;
    const observing = a.active || t.active;
    const answering = s.pending || s.activeResponseIds?.length || answers.running;
    const interacting = answering || i.pendingInputs || i.classifying || prep.length;
    const memorizing = work.running || m.consolidating;
    const diaryBusy = dj.running || dj.queued;
    const observationError = a.providerError || t.providerError;
    const oStatus = !data ? '等待状态' : observationError ? '观察服务异常' : !o.enabled && !t.active ? '未开启' : !o.connected ? '等待连接' : observing ? '分析画面中' : frameFresh ? '等待采样' : '等待画面';
    const iStatus = !data ? '等待状态' : answering ? '生成回答中' : interacting ? (phases[prep.at(-1)?.phase] || '处理请求中') : answers.queued ? '回答排队中' : '等待输入';
    const mStatus = !data ? '等待状态' : memorizing ? '整理记忆中' : diaryBusy ? '整理日记中' : work.queued ? '记忆排队中' : !m.enabled ? '记忆已关闭' : '等待新记录';
    const definitions = [
      {id:'observation', name:'现场观察', state:oStatus, active:observing, sub:'画面 → 现场证据'},
      {id:'interaction', name:'用户交互', state:iStatus, active:interacting, sub:'请求 → 回答 / 行动'},
      {id:'memory', name:'记忆整理', state:mStatus, active:memorizing || diaryBusy, sub:'证据 → 记忆 / 日记'}
    ];
    paint('executionLanes', definitions.map(lane => `<a href="#/lanes/${lane.id}" data-lane-link="summary-${lane.id}" data-lane="${lane.id}" data-state="${lane.active?'active':'idle'}"><b><i aria-hidden="true"></i>${lane.name}</b><span class="lane-status">${escape(lane.state)}</span><small>${lane.sub}</small></a>`).join(''), session);
    const shell = (lane, content) => `<article id="lane-${lane.id}" data-lane="${lane.id}" data-state="${lane.active?'active':'idle'}"><div class="lane-card-heading"><h2>${lane.name}</h2><span class="lane-status">${escape(lane.state)}</span></div>${content}</article>`;
    const cards = window.memoryRecords ? memoryRecords(state?.episodicMemory || {}) : [
      ...(state?.episodicMemory?.timelineViews?.participant?.recent || []),
      ...(state?.episodicMemory?.timelineViews?.case?.recent || [])];
    const inputTurn = latestTurn?.transcript || latestTurn?.text || latestTurn?.input?.text;
    const observationHTML = `<p class="lane-purpose">接收采样画面，生成有时间与来源的现场证据；与用户当前需要相关时，交给交互链路提醒。</p><p>${state?.ambient?.remindersEnabled!==false?'必要提醒已开启':'必要提醒已关闭'} · ${state?.ambient?.questionsEnabled?'关键信息不确定时先询问':'不主动询问'}</p>${stages([['接收画面',frameFresh?'done':'wait'],['独立分析',observing?'active':o.processedThrough?'done':'wait'],['现场证据',o.processedThrough?'done':'wait']])}
      <dl><dt>最近输入</dt><dd>${escape(time(o.cameraAt))}${o.cameraAt && !frameFresh?' · 历史画面':''}</dd><dt>已分析至</dt><dd>${escape(time(o.processedThrough))}</dd><dt>待处理采样</dt><dd>${a.pendingFrame?'有最新帧待分析':'无'}</dd><dt>任务观察</dt><dd>${t.active?'分析中':state?.ambient?.taskActive?'等待采样':'未开启任务观察'}</dd></dl>
      ${observationError?`<p class="lane-error">${escape(observationError.message || observationError.code || '观察提供方暂不可用，请检查设置与诊断。')}</p>`:''}<h3>最近结果</h3><p class="lane-result">${escape(observed?.summary || '尚无已分析的画面；授权观察并启用设备后开始。')}</p>
      <p class="lane-empty">画面按间隔采样，不代表连续观察。</p><a href="#/conversation" data-lane-link="camera">查看设备与观察授权 →</a>`;
    const interactionHTML = `<p class="lane-purpose">理解文字或语音，按需核对现场、检索记忆并回应。</p>${stages([['理解请求',i.classifying?'active':latestTurn?.semantic?'done':'wait'],['检索 / 核对',prep.some(row=>['waiting-vision','committing'].includes(row.phase))?'active':readForLatest?'done':'optional'],['回答 / 行动',answering?'active':answeredLatest?'done':'wait']])}
      <dl><dt>输入排队</dt><dd>${count(i.pendingInputs)}</dd><dt>正在理解</dt><dd>${count(i.classifying)}</dd><dt>后台回答</dt><dd>${count(answers.running)} 运行 · ${count(answers.queued)} 等待</dd><dt>历史失败</dt><dd>${count(answers.failed)} 条</dd></dl>
      <h3>最近输入</h3><p class="lane-result">${escape(inputTurn || '尚无用户请求')}</p><h3>当前处理</h3>${jobs(prep)}<h3>最近交付</h3><p class="lane-result">${escape(deliveredText || (latestAnswer?'请求已交付，可在对话记录查看。':'尚无已完成回答'))}</p>
      <a href="#/conversation" data-lane-link="conversation">打开对话与结果 →</a>`;
    const memoryHTML = `<p class="lane-purpose">保存获准的证据，异步整理记忆、事件与账户日记。</p>${stages([['接收证据',cards.length || work.completed || memorizing || work.queued?'done':'wait'],['解析 / 整理',memorizing?'active':work.completed?'done':'optional'],['保存记忆',cards.length?'done':'wait']])}
      <dl><dt>当前会话记忆</dt><dd>${m.enabled?'已开启':'已关闭'}</dd><dt>会话后台任务</dt><dd>${count(work.running)} 运行 · ${count(work.queued)} 等待</dd><dt>历史失败</dt><dd>${count(work.failed)} 条</dd><dt>最近记忆读取</dt><dd>${m.lastRead?escape(statuses[m.lastRead.status] || (m.lastRead.status==='degraded'?'部分读取失败':m.lastRead.status))+' · '+escape(time(m.lastRead.timestamp)):'暂无'}</dd></dl>
      <h3>会话整理队列</h3>${jobs(m.activeJobs || [])}<h3>最近保存</h3><p class="lane-result">${escape(cards[0]?.text || cards[0]?.summary || '暂无已保存记忆')}</p><div class="lane-diary"><h3>个人日记 · 全部会话</h3><p>${!diary?'未启用账户日记服务':diary.status==='unavailable'?'日记状态暂不可用':`${count(dj.running)} 运行 · ${count(dj.queued)} 等待 · ${count(dj.completed)} 已归档 · ${count(dj.failed)} 历史失败`}</p>${jobs(diary?.activeJobs || [])}<a href="diary.html" target="_blank" rel="noopener" data-lane-link="diary">阅读、补记与重试 →</a></div>
      <a href="#/conversation" data-lane-link="memory">查看已保存记忆 →</a>`;
    paint('laneDetails', definitions.map((lane,index)=>shell(lane,[observationHTML,interactionHTML,memoryHTML][index])).join(''), session);
    if (!sameSession) document.getElementById('laneDetails')?.querySelectorAll('details').forEach(node=>node.open=false);
    freshness();
  }
  function freshness() {
    const node = document.getElementById('laneSnapshotStatus');
    if (!node) return;
    const stale = !updated || Date.now()-updated > 12000;
    node.textContent = !updated?'等待服务状态':stale?'状态更新中断 · 下方为上次快照':'最近同步 '+new Date(updated).toLocaleTimeString();
    document.getElementById('executionLanes')?.classList.toggle('is-stale',stale);
    document.getElementById('laneDetails')?.classList.toggle('is-stale',stale);
  }
  setInterval(freshness,3000);
  return {render};
})();
