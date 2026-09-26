/* Actual invocation stages; configuration and aggregate response labels are not calls. */
function turnModelStages(turn, allEvents) {
  const kind = e => e.eventType || e.type;
  const belongs = e => e.payload?.turnId === turn.turnId || e.payload?.origin?.turnId === turn.turnId;
  const ids = new Set([turn.answer?.responseId].filter(Boolean));
  allEvents.filter(belongs).forEach(e => {
    if (e.responseId || e.payload?.responseId) ids.add(e.responseId || e.payload.responseId);
  });
  const events = allEvents.filter(e => belongs(e) || ids.has(e.payload?.responseId || e.responseId));
  const stages = [];
  const add = (stage, model) => {
    if (model && !stages.some(s => s.stage === stage && s.model === model)) stages.push({stage, model});
  };
  if (turn.semantic?.model) add('判断', turn.semantic.model);
  events.forEach(e => {
    const p = e.payload || {}, type = kind(e);
    if (type === 'scene.question.result') add(p.directAnswer ? '视觉理解／候选回答' : '视觉取证', p.model);
    if (type === 'scene.history.result') add('历史画面取证', p.model);
    if (type === 'answer.result') add('后台回答', p.model);
    if (type === 'provider.pipeline.metrics' && p.apiCalls > 0) {
      const label = {text: '文字回答', vision: '视觉理解', tts: '语音合成'}[p.stage];
      if (label) add(label, p.model);
    }
    if (type === 'response.dispatched' && p.backend === 'visual-answer-and-tts') add('视觉直接回答', p.model);
    // Native Omni has no modular text/TTS stages. Only an attributed created response proves use.
    if (type === 'response.created' && /omni/i.test(p.model || e.model || '')) add('实时回答', p.model || e.model);
  });
  if (stages.some(s => s.stage === '视觉直接回答')) {
    return stages.filter(s => s.stage !== '视觉理解／候选回答');
  }
  return stages;
}

function turnModelStageText(turn, events) {
  return turnModelStages(turn, events).map(s => s.stage + '：' + s.model).join(' · ');
}

const jevLabel = value => ({
  reply: '回答', text: '文字回答', remember: '保存记忆', recall: '查找记忆',
  task: '任务', structured: '结构化处理', stop: '停止播报', cancel: '取消回答',
  pause: '暂停', resume: '继续回答', keep: '继续当前播报', wait: '等待',
  other: '旁听记录', uncertain: '不确定', none: '无', assistant: '对助手',
  'foreground-dialogue': '回答对话', 'no-match': '未匹配', classified: '已分类',
  user: '对本人', speech: '语音', silent: '不插话', text_only: '仅文字',
}[value] || value || '未返回');

function jevAnswer(semantic, key) {
  return semantic?.requestMetrics?.decisions?.answers?.[key] || semantic?.jevChoices?.[key] || {};
}

function jevRouteSummary(semantic) {
  if (!semantic) return '';
  const route = semantic.jevRoute || jevAnswer(semantic, 'route').choice || semantic.route || semantic.intentKind;
  const interaction = semantic.interaction || {};
  const parts = [jevLabel(route)];
  if (interaction.audience || interaction.action || interaction.output) {
    parts.push([interaction.audience, interaction.action, interaction.output]
      .filter(Boolean).map(jevLabel).join(' · '));
  }
  if (semantic.memoryQuery || semantic.memoryQuerySpec || semantic.memoryKind) parts.push('记忆检索');
  if (semantic.memoryText || (semantic.memoryEvents && semantic.memoryEvents.length)) parts.push('记忆候选');
  if (semantic.selectedSkill || semantic.skillControl) parts.push('技能：' + (semantic.selectedSkill || semantic.skillControl));
  if (semantic.taskControl || semantic.selectedTask) parts.push('任务控制');
  if (semantic.speechControl) parts.push('语音控制');
  return parts.join(' · ');
}

function jevRouteDetails(semantic) {
  if (!semantic) return '';
  const answers = semantic.requestMetrics?.decisions?.answers || semantic.jevChoices || {};
  const choices = Object.entries(answers).map(([key, value]) => {
    if (!value || !value.choice) return null;
    const probabilities = Object.entries(value.probabilities || {})
      .filter(([, score]) => Number(score) > 0)
      .sort((a, b) => b[1] - a[1]).slice(0, 3)
      .map(([name, score]) => jevLabel(name) + ' ' + Math.round(Number(score) * 100) + '%')
      .join('，');
    return key + '：' + jevLabel(value.choice) + (probabilities ? '（' + probabilities + '）' : '');
  }).filter(Boolean);
  return [
    '模型：' + (semantic.model || '未知'),
    '方法：' + (semantic.method || '未知'),
    '路由：' + jevRouteSummary(semantic),
    choices.length ? 'JEV选择：' + choices.join('；') : '',
    semantic.skillStatus ? '技能匹配：' + jevLabel(semantic.skillStatus) : '',
    semantic.jevStructuredReason ? '转结构化处理：' + semantic.jevStructuredReason : '',
    semantic.latencyMs != null ? 'JEV耗时：' + semantic.latencyMs + ' ms' : '',
    semantic.componentErrors?.length ? '组件错误：' + semantic.componentErrors.join('；') : '',
  ].filter(Boolean).join('\n');
}
