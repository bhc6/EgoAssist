/* Stable controls: live telemetry never replaces inputs, buttons or unsaved drafts. */
class AmbientPanel {
  constructor(root, actions) {
    this.root = root;
    this.actions = actions;
    this.scope = null;
    this.busy = false;
    this.dirty = false;
    this.operation = 0;
    this.renderedResult = null;
    root.innerHTML = `<div class="ambient-heading"><b id="ambientTitle">后台环境观察</b><span id="ambientLiveStatus" role="status"></span><button id="ambientGrant" type="button">授权开启</button><button id="ambientRevoke" type="button" hidden>停止后台观察</button></div>
      <p id="ambientPurpose">让助手理解你眼前的情况：回答现场问题、记录有依据的经历，并在与你当前目标相关时提醒。</p>
      <p id="ambientNext" class="ambient-next"></p>
      <div class="ambient-feedback"><span id="ambientControlStatus" role="status" aria-live="polite"></span><button id="ambientOpenDevices" type="button" hidden>打开设备设置</button></div>
      <details id="ambientSettings"><summary>观察方式与设置 <span id="ambientDirty"></span></summary>
        <p>授权后按间隔分析摄像头画面。观察授权适用于本账号全部会话；以下设置只应用于当前会话。不开麦克风也能观察。</p>
        <form id="ambientSettingsForm"><label>观察间隔（秒）<input id="ambientInterval" type="number" min="2" max="120" step="1" required></label>
          <label class="ambient-check"><input id="ambientReminders" type="checkbox">需要时主动提醒</label>
          <label class="ambient-check"><input id="ambientQuestions" type="checkbox">关键信息不确定时先询问</label>
          <label>主动播报最短间隔（秒）<input id="ambientQuestionCooldown" type="number" min="1" max="3600" step="1" required></label>
          <label class="ambient-goal">希望观察帮你留意什么（可选）<textarea id="ambientReminderGoal" rows="2" maxlength="600" placeholder="例如：看到钥匙时提醒我收进包里"></textarea></label>
          <button id="ambientSave" type="submit">应用设置</button><button id="ambientDiscard" type="button">撤销未保存修改</button></form>
        <p>默认需要时提醒，不主动询问。开启询问后，仅在不确定的信息会影响当前帮助时问一句，例如“桌角那个是你的钥匙吗？”，不会因普通变化追问。关闭两项即可只记录。目标保存后持续用于本会话观察，清空即撤销；这不是定时提醒。空闲时播报，同一事项不反复提醒。采样可能漏看，不保证识别所有需要。</p>
      </details>
      <details id="ambientResults"><summary>最近观察与依据 <span id="ambientResultTime"></span></summary><p id="ambientLatest"></p><div id="ambientEvidence"></div><div id="ambientReviews"></div></details>`;
    this.$ = selector => root.querySelector(selector);
    this.$('#ambientGrant').onclick = () => this.act('consent', true);
    this.$('#ambientRevoke').onclick = () => this.act('consent', false);
    this.$('#ambientOpenDevices').onclick = () => actions.devices();
    this.$('#ambientSettingsForm').onsubmit = event => {event.preventDefault(); this.act('settings');};
    this.$('#ambientSettingsForm').oninput = () => {if(this.pendingControl?.kind==='settings')this.pendingControl=null;this.dirty=true;this.feedback('设置已修改，点击“应用设置”保存。');this.controls();};
    this.$('#ambientDiscard').onclick = () => {this.dirty=false;this.values(this.info);this.feedback('已恢复服务器设置。');this.controls();};
  }
  values(info = {}) {
    this.$('#ambientInterval').value = String(info.intervalSeconds ?? 5);
    this.$('#ambientQuestions').checked = Boolean(info.questionsEnabled);
    this.$('#ambientReminders').checked = info.remindersEnabled !== false;
    this.$('#ambientReminderGoal').value = info.reminderGoal || '';
    this.$('#ambientQuestionCooldown').value = String(info.questionCooldownSeconds ?? 90);
  }
  feedback(message, error = false) {
    this.$('#ambientControlStatus').textContent = message;
    this.$('#ambientControlStatus').classList.toggle('error',error);
  }
  controls() {
    this.$('#ambientGrant').disabled = this.busy;
    this.$('#ambientRevoke').disabled = this.busy;
    for(const node of this.$('#ambientSettingsForm').elements)node.disabled=this.busy;
    this.$('#ambientQuestionCooldown').disabled = this.busy || !(this.$('#ambientQuestions').checked || this.$('#ambientReminders').checked);
    this.$('#ambientDiscard').disabled = this.busy || !this.dirty;
    this.$('#ambientDirty').textContent = this.dirty ? '· 未保存' : '';
    this.$('#ambientSave').textContent = this.busy ? '处理中…' : '应用设置';
  }
  reset() {
    this.operation++;this.scope=null;this.info=null;this.busy=false;this.dirty=false;
    this.pendingControl=null;
    this.feedback('');this.root.hidden=true;
    this.$('#ambientLatest').textContent='';this.$('#ambientEvidence').replaceChildren();this.$('#ambientReviews').replaceChildren();
    this.renderedResult=null;
  }
  render(info, scope, reviews = '') {
    if(this.scope!==scope){this.reset();this.scope=scope;this.values(info);this.$('#ambientSettings').open=false;this.$('#ambientResults').open=false;}
    this.info=info;
    if(this.pendingControl && !this.busy && info){
      const pending=this.pendingControl;
      const confirmed=Object.entries(pending.expected).every(([key,value])=>info[key]===value);
      if(confirmed){
        if(pending.kind==='settings')this.dirty=false;
        this.feedback(pending.kind==='settings'?'连接已恢复，已确认设置生效。':info.consented?'连接已恢复，已确认观察授权开启。':'连接已恢复，已确认后台观察停止。');
      }else{
        this.feedback('连接已恢复，但尚未确认刚才的操作生效；请核对当前状态后重试。',true);
      }
      this.pendingControl=null;
    }
    this.root.hidden=!info?.enabled;
    if(!info?.enabled)return;
    const consent=Boolean(info.consented), frameFresh=info.cameraAt && Date.now()-Date.parse(info.cameraAt)<10000;
    const failure=info.lane?.providerError;
    this.root.className='ambient-banner ambient-panel '+(consent?'authorized':'idle');
    this.$('#ambientTitle').textContent=info.watchlist?.mode==='focused'?(info.watchlist.title || '专项环境观察'):'后台环境观察';
    this.$('#ambientLiveStatus').textContent=!consent?'已停止':failure?'服务异常':!info.connected?'已授权 · 等待连接':info.processing?'正在分析画面':!frameFresh?'已授权 · 等待画面':'正在按间隔观察';
    this.$('#ambientGrant').hidden=consent;this.$('#ambientRevoke').hidden=!consent;
    this.$('#ambientNext').textContent=!consent?'不会自动分析新画面。主动提问和任务内画面核对仍可使用。':
      failure?'观察服务暂不可用，请检查设置与诊断。':!info.connected || !frameFresh?'还没有持续输入画面；打开设备设置、启用摄像头并连接实时会话后开始。':
      `当前会话约每 ${info.intervalSeconds} 秒分析一帧 · ${info.remindersEnabled!==false?'需要时提醒':'提醒已关闭'} · ${info.questionsEnabled?'必要时先询问':'不主动询问'}。`;
    this.$('#ambientOpenDevices').hidden=!consent || Boolean(info.connected && frameFresh);
    if(!this.dirty && !this.busy)this.values(info);
    this.controls();
    const latest=info.observation;
    this.$('#ambientLatest').textContent=latest?.summary || '尚无已分析画面。';
    this.$('#ambientResultTime').textContent=latest?.capturedAt?'· '+new Date(latest.capturedAt).toLocaleTimeString():'';
    const signature=JSON.stringify(latest || null);
    if(signature!==this.renderedResult){
      this.renderedResult=signature;
      const root=this.$('#ambientEvidence');root.replaceChildren();
      if(latest?.uncertainties?.length){const p=document.createElement('p');p.textContent='待确认：'+latest.uncertainties.join('；');root.append(p);}
      if(latest?.media?.assetId){const a=document.createElement('a');a.target='_blank';a.rel='noopener';a.textContent='查看原始采样帧';a.href='/api/scene/media?session='+encodeURIComponent(this.actions.session())+'&asset='+encodeURIComponent(latest.media.assetId);root.append(a);}
      if(latest){const details=document.createElement('details'),summary=document.createElement('summary'),pre=document.createElement('pre');summary.textContent='原始观察记录';pre.textContent=JSON.stringify(latest,null,2);details.append(summary,pre);root.append(details);}
    }
    updateDisclosureHTML(this.$('#ambientReviews'),reviews,scope);
  }
  async act(kind, granted) {
    if(this.busy)return;
    const scope=this.scope, operation=++this.operation;
    this.pendingControl=null;
    let settings;
    if(kind==='settings'){
      if(!this.$('#ambientSettingsForm').reportValidity())return;
      settings={intervalSeconds:Number(this.$('#ambientInterval').value),questionsEnabled:this.$('#ambientQuestions').checked,
        remindersEnabled:this.$('#ambientReminders').checked,reminderGoal:this.$('#ambientReminderGoal').value,
        questionCooldownSeconds:Number(this.$('#ambientQuestionCooldown').value)};
    }
    this.busy=true;this.controls();this.feedback(kind==='settings'?'正在保存设置…':granted?'正在开启观察…':'正在停止后台观察…');
    try {
      await (kind==='settings'?this.actions.settings(settings):this.actions.consent(granted));
      if(this.scope!==scope || this.operation!==operation)return;
      if(kind==='settings')this.dirty=false;
      this.feedback(kind==='settings'?'设置已保存，下次采样按新间隔执行。':granted?'观察已授权；有摄像头输入时开始。':'已停止全部会话的后台观察。已保存记录保留，设备和主动对话未关闭。');
    }catch(error){
      if(this.scope===scope && this.operation===operation){
        if(error.connectionUncertain)this.pendingControl={kind,expected:kind==='settings'?{...settings,reminderGoal:settings.reminderGoal.trim()}:{consented:granted}};
        this.feedback(error.message,true);
      }
    }finally{
      if(this.scope===scope && this.operation===operation){this.busy=false;if(!this.dirty)this.values(this.info);this.controls();}
    }
  }
}
