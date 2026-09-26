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
