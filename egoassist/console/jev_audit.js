/* Read the selected turn's persisted decision; never substitute today's prompt. */
let jevAuditState = {key: null, status: 'idle'};
const jevQuestions = {
  route: '本轮请求如何处理？', details: '是否需要进一步结构化解析？',
  evidenceScope: '回答需要哪一范围的证据？', retention: '本轮内容允许怎样保存？',
  quiet: '是否改变持续静默设置？', taskControl: '是否控制当前任务？',
  skill: '是否进入或退出某个技能？', target: '操作指向哪个已有答案？', verdict: '独立复核是否支持这次操作？',
};
const jevReasons = {
  'evidence-scope-recall': '证据范围要求检索历史或已存记忆',
  'temporal-count-recall': '时间范围或次数问题必须查询历史',
  'pending-memory-draft': '需要补全已有记忆草稿', 'temporal-history-evidence': '需要解析历史时间范围',
  'structured-content': '需要提取结构化内容', 'ambiguous-details': '进一步提取的概率达到分流阈值',
  'structured-explicit-retention': '明确保存请求需要进一步解析',
  'inconsistent-memory-decisions': '保存路由与保留范围不一致',
  'source-and-control': '个人陈述同时包含控制操作', 'compound-task-control': '包含复合任务控制',
};
const jevReason = value => jevReasons[value] || value;
const jevMs = value => typeof value === 'number' && Number.isFinite(value) ? value + ' ms' : '未记录';
const jevProbability = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 && value <= 1
  ? Number((value * 100).toPrecision(6)) + '%' : '未返回';

function jevQuestionHTML(key, question, answer, stageName) {
  question = question || {}; answer = answer || {};
  const criteria = question.criteria || {}, probabilities = answer.probabilities || {};
  const choices = [...new Set([...Object.keys(criteria), ...Object.keys(probabilities), ...(answer.choice ? [answer.choice] : [])])];
  const rows = choices.map(choice => {
    const selected = answer.choice === choice, probability = probabilities[choice];
    const valid = typeof probability === 'number' && Number.isFinite(probability) && probability >= 0 && probability <= 1;
    return '<tr' + (selected ? ' class="jev-selected"' : '') + '><th scope="row"><code>' + esc(choice) + '</code>' +
      (selected ? '<strong class="jev-choice-badge">模型选择</strong>' : '') + '</th><td>' +
      esc(criteria[choice] ?? '历史日志未记录选项说明') + '</td><td><span class="jev-probability">' +
      esc(jevProbability(probability)) + '</span>' + (valid ? '<span class="jev-probability-track" aria-hidden="true"><span style="width:' +
      (probability * 100) + '%"></span></span>' : '') + '</td></tr>';
  }).join('');
  return '<details class="jev-question" data-disclosure="jev-' + esc(stageName + '-' + key) + '"' + (key === 'route' ? ' open' : '') +
    '><summary>' + esc(jevQuestions[key] || key) + ' <code>' + esc(key) + '</code> · ' +
    esc(answer.choice ? '选择 ' + answer.choice : '未返回选择') + '</summary>' +
    '<details class="jev-instructions" data-disclosure="jev-instructions-' + esc(stageName + '-' + key) + '"><summary>实际判断问题与完整指令</summary><p>' +
    esc(question.instructions || '这条历史日志没有保存判断问题，不能使用当前模板代替。') + '</p></details>' +
    (answer.confidence != null ? '<p>供应商返回的 confidence：' + esc(answer.confidence) + '</p>' : '') +
    (choices.length ? '<div class="jev-table-wrap"><table class="jev-choices"><thead><tr><th scope="col">Choice</th><th scope="col">选项含义</th>' +
    '<th scope="col">概率</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="empty-state">没有记录选项及概率。</p>') + '</details>';
}

function jevAuditHTML(semantic) {
  if (!semantic) return '<p class="empty-state">尚无分类记录。</p>';
  const metrics = semantic.requestMetrics || {}, audit = metrics.decisions || {};
  const row = (label, value) => '<div class="workflow-fact"><b>' + esc(label) + '</b><span>' + esc(value) + '</span></div>';
  const show = value => typeof value === 'string' ? value : JSON.stringify(value);
  const choice = audit.answers?.route?.choice || semantic.jevChoices?.route?.choice;
  let html = '<div class="jev-audit">' + row('分类状态', jevLabel(semantic.status)) +
    row('模型原始路由', choice ? choice + ' · ' + jevLabel(choice) : '未返回') +
    row('最终路由结果', jevRouteSummary(semantic)) +
    row('证据范围', jevLabel(audit.evidenceScope || semantic.evidenceScope)) +
    row('JEV 模型', audit.resolvedModel || audit.model || (semantic.routerBackend === 'openrouter-decisions' ? semantic.modelsUsed?.[0] : null) || '未记录') +
    row('提示词版本', audit.promptVersion || '未记录') +
    row('分类总耗时', jevMs(semantic.latencyMs)) + row('JEV 请求耗时', jevMs(audit.latencyMs));
  for (const [key, label] of [['preparationMs', '上下文准备'], ['generationMs', '主分类阶段'], ['httpMs', '结构化请求 HTTP'], ['deadlineOverrunMs', '超出截止时间']]) {
    if (metrics[key] != null) html += row(label, jevMs(metrics[key]));
  }
  html += '<p class="jev-note">各阶段计时可能重叠，不应相加。选项概率不代表事实可信度。</p>';
  for (const key of ['routeOverride', 'structuredRouteOverride']) if (audit[key]) html += row(key === 'routeOverride' ? '路由调整原因' : '解析后调整', jevReason(audit[key]));
  if (metrics.structuredReason || semantic.jevStructuredReason) html += row('追加解析原因', jevReason(metrics.structuredReason || semantic.jevStructuredReason));
  if (semantic.modelsUsed?.length) html += row('记录的调用模型', semantic.modelsUsed.join(' → '));
  if (audit.targetUnavailable) html += row('答案目标', '没有匹配到目标，未自动选择其他答案');
  if (audit.resumeNeedsContext) html += row('续答上下文', '缺少已有答案，需要补充上下文');
  if (audit.retentionCeiling) html += row('保存范围限制', jevLabel(audit.retentionCeiling));
  if (semantic.protocolNormalizations?.length) html += row('协议调整', semantic.protocolNormalizations.join('；'));
  if (semantic.componentErrors?.length) html += row('组件错误', semantic.componentErrors.map(show).join('；'));
  if (semantic.error) html += row('分类错误', semantic.error);
  if (semantic.timedOut) html += row('超时', '是');
  if (semantic.providerError) html += row('供应商错误', show(semantic.providerError));
  const stages = [['decisions', 'JEV 主判断'], ['taskControlCheck', '任务控制独立复核'], ['cancellationCheck', '取消答案独立复核']];
  for (const [name, title] of stages) {
    const stage = metrics[name];
    if (!stage) continue;
    html += '<section class="jev-stage"><h4>' + esc(title) + '</h4>';
    if (name !== 'decisions') html += row('复核模型', stage.resolvedModel || stage.model || '未记录') + row('复核耗时', jevMs(stage.latencyMs));
    if (stage.httpStatus != null) html += row('HTTP 状态', stage.httpStatus);
    if (stage.requestId) html += row('请求 ID', stage.requestId);
    if (stage.attempts != null) html += row('请求尝试次数', stage.attempts);
    if (stage.retry) html += row('重试记录', show(stage.retry));
    if (stage.circuitOpen) html += row('请求状态', '供应商故障熔断，未继续请求');
    if (stage.error || stage.providerError) html += row('请求错误', show(stage.providerError || stage.error));
    const questions = stage.questions || stage.request?.questions || {}, answers = stage.answers || {};
    const keys = [...new Set([...Object.keys(questions), ...Object.keys(answers)])];
    html += keys.map(key => jevQuestionHTML(key, questions[key], answers[key], name)).join('') || '<p class="empty-state">本阶段没有保存判断题或返回选项。</p>';
    html += '</section>';
  }
  if (!metrics.decisions) {
    const answers = semantic.jevChoices || {};
    html += Object.keys(answers).map(key => jevQuestionHTML(key, null, answers[key], 'legacy')).join('');
  }
  return html + '<details data-disclosure="jev-raw"><summary>原始路由审计字段</summary><pre>' + esc(JSON.stringify(semantic, null, 2)) + '</pre></details></div>';
}

function jevAuditPanel(turn) {
  const key = selectedSessionId + ':' + turn.turnId + ':' + (turn.semantic?.auditEventId || 'legacy');
  if (jevAuditState.key !== key) jevAuditState = {key, status: 'idle'};
  if (jevAuditState.status === 'loaded') return jevAuditHTML(jevAuditState.entry.decision);
  if (!turn.semantic?.auditEventId) return jevAuditHTML(turn.semantic);
  if (jevAuditState.status === 'error') return '<p role="alert">' + esc(jevAuditState.error) + '</p><button type="button" data-load-jev>重试加载判断题</button>';
  return '<p aria-live="polite">' + (jevAuditState.status === 'loading' ? '正在读取本轮实际判断题和全部选项…' : '展开后读取本轮实际判断题、所有 choice 及概率。') + '</p>';
}

function bindJevAudit(root, turn) {
  const details = root.querySelector('[data-disclosure="semantic"]');
  if (!details || !turn.semantic?.auditEventId) return;
  const key = jevAuditState.key, session = selectedSessionId, eventId = turn.semantic.auditEventId;
  const load = async () => {
    if (jevAuditState.key !== key || ['loading', 'loaded'].includes(jevAuditState.status)) return;
    jevAuditState.status = 'loading'; renderTurnWorkflow();
    try {
      const response = await fetchJSON('/api/prompts?session=' + encodeURIComponent(session) + '&event=' + encodeURIComponent(eventId));
      if (jevAuditState.key !== key || selectedSessionId !== session) return;
      if (response.sessionId !== session || response.entry?.id !== eventId || response.entry?.turnId !== turn.turnId) throw Error('路由记录与所选对话不一致');
      jevAuditState = {key, status: 'loaded', entry: response.entry};
    } catch (error) {
      if (jevAuditState.key !== key || selectedSessionId !== session) return;
      jevAuditState = {key, status: 'error', error: error.message};
    }
    renderTurnWorkflow();
  };
  details.ontoggle = () => { if (details.open && jevAuditState.status === 'idle') load(); };
  details.querySelector('[data-load-jev]')?.addEventListener('click', load);
  if (details.open && jevAuditState.status === 'idle') load();
}
