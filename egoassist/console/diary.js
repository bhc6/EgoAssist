/* Account archive: no camera, microphone, realtime socket or runtime actions. */
(() => {
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels = {daily:'日记', weekly:'周回顾', monthly:'月回顾'};
  const states = {queued:'等待整理', running:'正在整理', completed:'已归档', empty:'暂无记录', failed:'整理未完成', cancelled:'已取消', stale:'需要重新整理'};
  const sections = {overview:'这段时间', life:'生活', work:'工作', changes:'进展与变化', pending:'还没完成的事', suggestions:'下一步可以做什么'};
  const sourceKinds = {'user-statement':'你的原话','sampled-observation':'画面观察','execution-receipt':'执行回执','job-outcome':'任务结果','assistant-delivery':'助手交付的文字','memory-state':'记忆状态','personal-note':'个人补记','historical-dialogue':'历史对话'};
  let entries = [], cursor = null, selected = null, current = null, timer = null;
  let generation = 0, listGeneration = 0, noteGeneration = 0, archiveFilter = null, userId = null;
  let readingRequest = null;
  const active = row => ['queued','running'].includes(row?.status);
  function status(text, error = false) {
    $('#status').textContent = text;
    $('#notice').classList.toggle('error', error);
  }
  function resetAccount() {
    clearTimeout(timer); generation++; listGeneration++; noteGeneration++;
    readingRequest?.abort(); readingRequest=null;
    entries = []; cursor = selected = current = userId = null; archiveFilter = null;
    $('#archive').replaceChildren(); $('#reading').replaceChildren(); $('#savedNotes').replaceChildren();
    $('#noteText').value = ''; $('#app').hidden = true; $('#login').hidden = false;
  }
  async function api(path, body, signal) {
    const timeout=AbortSignal.timeout(15000);
    const response = await fetch('/api/' + path, {credentials:'same-origin', signal:signal ? AbortSignal.any([signal,timeout]) : timeout,
      ...(body === undefined ? {} : {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)})});
    const data = await response.json().catch(() => null);
    if (response.status === 401) {
      resetAccount();
      throw Error('登录已过期，请重新登录。');
    }
    if (!response.ok) throw Error(data?.error || data?.message || (response.status === 404 ? '日记服务未开启，请检查服务配置后重试。' : '日记服务暂不可用，请稍后重试。'));
    if (!data) throw Error('服务返回格式异常，请重试。');
    return data;
  }
  async function guarded(fn, button) {
    if (button) button.disabled = true;
    try { await fn(); }
    catch (error) {
      status(error.name === 'TimeoutError' ? '连接超时，已提交的整理会继续保留；可重试读取。' : error.message, true);
      $('#retryLoad').hidden = false;
    } finally { if (button?.isConnected) button.disabled = false; }
  }
  function range(period) {
    const format = value => new Intl.DateTimeFormat('sv-SE', {timeZone:period.timezone,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));
    const first = format(period.start), last = format(Date.parse(period.end)-1);
    return first + (last === first ? '' : ' — ' + last);
  }
  function archive() {
    $('#archive').innerHTML = entries.map(row => `<button class="entry" data-id="${esc(row.id)}" aria-current="${row.id===selected}"><strong>${labels[row.period.kind]} · ${esc(range(row.period))}</strong><small>${states[row.status] || esc(row.status)} · ${new Date(row.createdAt).toLocaleString()}</small></button>`).join('') || '<p>这段时间还没有日记，可整理已有记录或先写补记。</p>';
    $('#more').hidden = !cursor;
  }
  function selection() {
    // Keep the clicked button in place, including keyboard focus.
    $('#archive').querySelectorAll('[data-id]').forEach(button=>button.setAttribute('aria-current',String(button.dataset.id===selected)));
  }
  async function listing(more = false) {
    const epoch = ++listGeneration, params = new URLSearchParams(archiveFilter || {});
    if (more && cursor) params.set('before', cursor);
    const data = await api('diary?' + params);
    if (epoch !== listGeneration) return false;
    entries = more ? [...entries,...data.entries] : data.entries; cursor = data.next;
    archive(); $('#automatic').checked = data.schedule.enabled;
    $('#scheduleStatus').textContent = data.schedule.enabled ? '自动归档已开启 · ' + data.schedule.timezone : '自动归档未开启';
    return true;
  }
  function render(row) {
    const same = current?.id === row.id, root = $('#reading');
    const openSections = same ? new Set([...root.querySelectorAll('details[open][data-key]')].map(node => node.dataset.key)) : new Set();
    const focus = same && root.contains(document.activeElement) ? document.activeElement.closest('[data-key]')?.dataset.key : null;
    current = row;
    let html = `<p class="eyebrow">${labels[row.period.kind]}</p><h2>${esc(row.body?.title || states[row.status])}</h2><p class="period">${esc(range(row.period))} · ${esc(row.period.timezone)} · ${esc(states[row.status])}</p>`;
    if (row.snapshotAt) html += `<p class="period">整理快照：${esc(new Date(row.snapshotAt).toLocaleString())}</p>`;
    if (row.status==='stale') html += '<div class="coverage diary-error">这份旧档案的依据已变化，正文已撤下。点击下方“重新整理”，可根据当前仍保留的记录生成新版本。</div>';
    if (row.newRecords) html += `<div class="coverage new-records">快照后新增 ${Number(row.newRecords)} 条记录。当前版本仍可阅读；重新整理可纳入新内容。</div>`;
    if (active(row)) html += `<div class="diary-progress" role="status"><b>${esc(row.phase || '等待整理')}</b><p>已保存 ${row.calls?.length || 0} 次处理结果。可离开页面，回来后继续查看。</p></div>`;
    if (row.coverage) {
      const c = row.coverage, kinds = c.byKind || {};
      html += `<details class="coverage" data-key="coverage"><summary>依据 ${c.records} 条记录 · ${Object.keys(c.byDay || {}).length} 天 · 查看覆盖范围</summary><p>${Object.entries(kinds).map(([kind,n])=>esc(sourceKinds[kind] || kind)+' '+n+' 条').join(' · ')}</p><p>${c.noRecordDays?.length?'无记录日期：'+esc(c.noRecordDays.join('、'))+'。':''}${c.unplacedSources?.length?'另有 '+c.unplacedSources.length+' 条来源缺少明确时间。':''}无记录不代表没有活动；采样画面不代表连续观察。</p></details>`;
    }
    if (row.body) {
      const sources = new Map((row.sources || []).map(item=>[item.sourceId,item]));
      html += Object.entries(sections).map(([key,title])=>`<section><h3>${title}</h3>${(row.body[key] || []).length ? row.body[key].map((item,index)=>`<div class="claim"><p>${esc(item.text)}</p><details data-key="source-${key}-${index}"><summary>查看依据 · ${item.sourceIds.length} 条</summary>${item.sourceIds.map(ref=>{const source=sources.get(ref);return `<div class="source"><b>${esc(source?.localDate)} · ${esc(sourceKinds[source?.kind] || source?.kind)}</b><p>${esc(source?.text || '来源需重新读取')}</p><small>${esc(ref)}</small></div>`;}).join('')}</details>${key==='suggestions'?`<div class="feedback">${Object.entries({planned:'准备做',dismissed:'暂不采纳',done:'我已完成'}).map(([value,label])=>`<button data-feedback="${index}" data-state="${value}" aria-pressed="${row.feedback?.[index]===value}">${label}</button>`).join('')}</div>`:''}</div>`).join(''):'<p class="period">这部分暂无足够记录。</p>'}</section>`).join('');
      html += `<div class="actions"><a href="/api/diary/${encodeURIComponent(row.id)}/export">导出日记 Markdown</a><button class="secondary" data-regenerate>重新整理并保留新版本</button></div>`;
    } else {
      html += `${row.error?`<p class="diary-error">${esc(row.error)}</p>`:''}<p>${!active(row)?esc(row.phase || ''):''}</p><div class="actions">${active(row)?'<button class="secondary" data-cancel>取消本次整理</button>':['failed','cancelled'].includes(row.status)?'<button data-retry>继续整理 / 重试</button>':'<button data-regenerate>重新整理</button>'}</div>`;
    }
    if (row.notes?.length) html += '<h3>我的补记</h3>' + row.notes.map(note=>`<div class="claim"><small>${esc(note.day)}</small><p>${esc(note.text)}</p></div>`).join('');
    if (row.calls?.length) html += `<details data-key="calls"><summary>生成记录 · ${row.calls.length} 次处理</summary>${row.calls.map((call,index)=>`<details data-key="call-${call.callId || index}"><summary>${esc(call.model)} · ${esc(call.stage)} · ${esc(call.status || '未记录状态')}</summary><p>${call.request?'已保存实际输入；下方包含完整请求、来源映射及已收到的输出。':'历史调用未保存完整输入，无法用当前模板补造。'}</p><pre>${esc(JSON.stringify(call,null,2))}</pre></details>`).join('')}</details>`;
    root.innerHTML = html;
    root.querySelectorAll('[data-key]').forEach(node=>{node.open=openSections.has(node.dataset.key);if(node.dataset.key===focus)node.querySelector('summary')?.focus({preventScroll:true});});
    root.setAttribute('aria-busy', String(active(row)));
  }
  function schedule(id, epoch, delay = 2500) {
    clearTimeout(timer);
    timer = setTimeout(()=>{if(epoch===generation && selected===id) guarded(()=>open(id,false));},delay);
  }
  async function open(id, select = true) {
    if (select && selected===id && readingRequest) return;
    readingRequest?.abort();
    const request=new AbortController(); readingRequest=request;
    const epoch = ++generation; clearTimeout(timer); selected = id;
    if (select) {
      selection();
      const entry=entries.find(row=>row.id===id), root=$('#reading');
      root.innerHTML=`<div class="empty"><h2>${entry?esc(labels[entry.period.kind]+' · '+range(entry.period)):'正在打开日记'}</h2><p role="status">正在读取这份档案…</p></div>`;
      root.setAttribute('aria-busy','true');
      status('正在读取日记…');
      if (matchMedia('(max-width:760px)').matches) root.scrollIntoView({block:'start'});
    }
    try {
      const row = await api('diary/' + encodeURIComponent(id),undefined,request.signal);
      if (epoch !== generation) return;
      if (select) { $('#kind').value=row.period.kind; $('#anchor').value=row.period.anchor; $('#timezone').value=row.period.timezone; }
      const entry = entries.find(item=>item.id===id);
      if (entry) {entry.status=row.status;entry.phase=row.phase;}
      archive(); render(row); $('#retryLoad').hidden=true;
      const url=new URL(location.href);url.searchParams.set('entry',id);history.replaceState(null,'',url);
      status(active(row)?'正在后台整理，进度会自动更新。':states[row.status]+(row.newRecords?' · 有新内容可纳入':''));
      if (active(row)) schedule(id,epoch);
    } catch(error) {
      if(epoch!==generation)return;
      $('#reading').setAttribute('aria-busy','false');
      if(select) $('#reading').innerHTML='<div class="empty"><h2>这份档案暂时无法打开</h2><p>请点击上方“重试加载”，或选择另一份档案。</p></div>';
      // A transient read failure must not permanently stop background progress.
      if(userId && active(current))schedule(id,epoch,5000);
      throw error;
    } finally { if(readingRequest===request)readingRequest=null; }
  }
  async function generate(period) {
    const row = await api('diary',{kind:period?.kind || $('#kind').value, anchor:period?.anchor || $('#anchor').value, timezone:period?.timezone || $('#timezone').value});
    archiveFilter=null; await listing(); await open(row.id);
  }
  async function notes() {
    const epoch=++noteGeneration, day=$('#noteDay').value;
    if(!day)return;
    const data=await api('diary/notes?day='+encodeURIComponent(day));
    if(epoch!==noteGeneration)return;
    $('#savedNotes').innerHTML='<h3>'+esc(day)+' · 已保存补记</h3>'+(data.notes.map(note=>`<p class="saved-note">${esc(note.text)}</p>`).join('') || '<p class="period">这一天暂无补记。</p>');
  }
  async function boot() {
    const auth=await api('auth/me');
    if(!auth.user){resetAccount();status('请登录后查看个人日记。');return;}
    if(userId && userId!==auth.user.id)resetAccount();
    userId=auth.user.id; $('#login').hidden=true; $('#app').hidden=false;
    const selectionEpoch=generation;
    if(!await listing())return;
    await notes();
    if(selectionEpoch!==generation)return; // A click during startup owns the reading pane.
    const requested=new URLSearchParams(location.search).get('entry');
    if(requested || entries.length)await open(requested || entries[0].id);
    else status('选择日期整理已有记录，或先写一笔补记。');
    $('#retryLoad').hidden=true;
  }
  const local=new Date(Date.now()-new Date().getTimezoneOffset()*60000).toISOString().slice(0,10);
  $('#anchor').value=local; $('#noteDay').value=local;
  $('#timezone').value=Intl.DateTimeFormat().resolvedOptions().timeZone || 'Asia/Shanghai';
  $('#loginForm').onsubmit=event=>{event.preventDefault();guarded(async()=>{await api('auth/login',Object.fromEntries(new FormData(event.target)));$('#loginForm [name=password]').value='';await boot();},event.submitter);};
  $('#generate').onsubmit=event=>{event.preventDefault();guarded(()=>generate(),$('#generateButton'));};
  $('#archive').onclick=event=>{const button=event.target.closest('[data-id]');if(button)guarded(()=>open(button.dataset.id));};
  $('#more').onclick=()=>guarded(()=>listing(true),$('#more'));
  $('#findArchive').onclick=()=>guarded(async()=>{
    generation++;clearTimeout(timer);readingRequest?.abort();readingRequest=null;archiveFilter={kind:$('#kind').value,anchor:$('#anchor').value,timezone:$('#timezone').value};
    if(!await listing())return;
    if(entries.length)await open(entries[0].id);
    else {selected=current=null;$('#reading').innerHTML='<div class="empty"><h2>这段时间还没有日记</h2><p>点击“整理这段时间”，或先在下方补记。</p></div>';status('未找到已有日记。');}
  },$('#findArchive'));
  $('#allArchive').onclick=()=>guarded(async()=>{archiveFilter=null;await listing();status('正在查看全部日记档案。');},$('#allArchive'));
  $('#reading').onclick=event=>{
    const button=event.target.closest('button');if(!button || !current)return;
    const row=current, id=row.id;
    guarded(async()=>{
      if(button.hasAttribute('data-feedback')){const value=await api('diary/'+id+'/feedback',{item:Number(button.dataset.feedback),state:button.dataset.state});if(current?.id===id){current.feedback=value;render(current);status('已保存你的选择。');}}
      else if(button.hasAttribute('data-cancel') || button.hasAttribute('data-retry')){await api('diary/'+id+(button.hasAttribute('data-cancel')?'/cancel':'/retry'),{});if(selected===id)await open(id,false);}
      else if(button.hasAttribute('data-regenerate'))await generate(row.period);
    },button);
  };
  $('#noteForm').onsubmit=event=>{event.preventDefault();const text=$('#noteText').value, day=$('#noteDay').value;guarded(async()=>{
    await api('diary/notes',{day,text});if($('#noteText').value===text)$('#noteText').value='';await notes();
    if(selected)await open(selected,false);status('补记已保存。点击“整理这一天”可生成日记。');
  },event.submitter);};
  $('#noteDay').onchange=()=>guarded(notes);
  $('#noteGenerate').onclick=()=>guarded(()=>generate({kind:'daily',anchor:$('#noteDay').value,timezone:$('#timezone').value}),$('#noteGenerate'));
  $('#automatic').onchange=()=>guarded(async()=>{try{await api('diary/settings',{enabled:$('#automatic').checked,timezone:$('#timezone').value,kinds:['daily','weekly','monthly']});await listing();}catch(error){$('#automatic').checked=!$('#automatic').checked;throw error;}},$('#automatic'));
  $('#retryLoad').onclick=()=>guarded(async()=>{if(userId && selected){await listing();await open(selected,false);await notes();}else await boot();$('#retryLoad').hidden=true;},$('#retryLoad'));
  addEventListener('pageshow', event=>{if(event.persisted)guarded(boot);});
  guarded(boot);
})();
