/* Explain persisted output permission separately from observation enablement. */
function speechPermissionText(permission) {
  if (!permission) return '正在读取播报权限。';
  const parts = [];
  if (permission.quiet) {
    parts.push('会话免打扰正在阻止主动提醒和询问' + (permission.allowReplies ? '；主动提问仍可回答。' : '；回答也保持静默。'));
    const source = permission.permissionSource;
    if (source?.at) parts.push('设置时间：' + new Date(source.at).toLocaleString() + '。');
    if (source?.text) parts.push('触发原话：“' + source.text + '”。');
    parts.push(permission.until ? '到期：' + new Date(permission.until).toLocaleString() + '。' : '未设置到期时间，重连也会保留。');
  }
  if (permission.paused) parts.push('当前还在等待话语处理完成，暂时让出声音。');
  if (!parts.length) parts.push('会话允许播报；观察提醒仍需开启、证据有效且当前对话空闲。');
  return parts.join(' ');
}

function observationPermissionText(review, currentPermission) {
  if (review.reason !== 'output-not-permitted') return '';
  const recorded = review.outputPermission;
  let reason = '当时的会话播报权限阻止了这次提醒或询问。';
  if (recorded?.quiet) reason = '当时会话处于免打扰。';
  else if (recorded?.paused) reason = '当时正在等待话语处理，暂时让出声音。';
  if (recorded?.permissionSource?.text) reason += ' 触发原话：“' + recorded.permissionSource.text + '”。';
  return reason + ' 这是未播出的历史记录，不会自动补播旧画面提醒。' +
    (currentPermission?.quiet ? ' 当前仍处于免打扰，可在页面顶部解除。' :
      currentPermission?.paused ? ' 当前仍在等待话语处理。' :
        currentPermission ? ' 当前免打扰已解除，后续采样重新判断。' : '');
}
