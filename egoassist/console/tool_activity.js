/* Tool rows reflect journal evidence and never interrupt the conversation. */
class ToolActivityView {
  constructor(root) {
    this.root = root;
    this.session = null;
    this.calls = new Map();
    this.root.onclick = event => {
      const button = event.target.closest("[data-tool-artifact]");
      if (button) openArtifact(button.dataset.toolArtifact);
    };
  }
  reset(session) {
    if (this.session === session) return;
    this.session = session;
    this.calls.clear();
    this.render();
  }
  snapshot(session, calls) {
    this.reset(session);
    for (const call of calls || []) {
      if (this.calls.get(call.callId)?.result && !call.result && ["received", "running", "selecting"].includes(call.status)) continue;
      this.calls.set(call.callId, call);
    }
    this.render();
  }
  event(session, message) {
    this.reset(session);
    const previous = this.calls.get(message.callId) || {};
    if (previous.result && !message.result && ["received", "running", "selecting"].includes(message.status)) return;
    this.calls.set(message.callId, {...previous, ...message,
      status: message.type === "tool_confirmation_required" ? "recovering" : message.status});
    this.render();
  }
  render() {
    const labels = {received: "已接收", "waiting-confirmation": "等待自动恢复", recovering: "正在自动恢复",
      selecting: "正在判断工具调用", "selection-finished": "工具判断结束",
      running: "执行中", completed: "已完成", accepted: "已受理", scheduled: "已安排", queued: "排队中",
      resolved: "已找到资料", rejected: "已拒绝", failed: "执行失败", error: "执行失败",
      unsupported: "当前不可执行", "demo-only": "仅演示", interrupted: "执行中断，结果未确认"};
    const calls = [...this.calls.values()].reverse();
    this.root.hidden = !calls.length;
    const html = calls.map(call => {
      const detail = call.result?.error || call.result?.reason || call.result?.message || "";
      return "<article class='tool-call' data-call-id='" + esc(call.callId) + "'><header><b>" + esc(call.tool) +
        "</b><span>" + esc(labels[call.status] || call.status) + "</span></header>" +
        (call.description ? "<p>" + esc(call.description) + "</p>" : "") +
        (call.origin?.turnId ? "<small>对应问题：" + esc(call.origin.turnId) + "</small>" : "") +
        (Number.isFinite(call.latencyMs) ? "<small>模型判断：" + esc(call.latencyMs) + " ms</small>" : "") +
        (call.result?.artifact?.artifact_id ? "<p>" + esc(call.result.artifact.title || "参考资料") +
          " <button type='button' data-tool-artifact='" + esc(call.result.artifact.artifact_id) + "'>查看资料</button></p>" : "") +
        "<details data-disclosure='" + esc(call.callId) + "'><summary>参数与执行记录</summary><pre>" +
        esc(JSON.stringify({callId:call.callId, arguments:call.arguments, origin:call.origin, request:call.request, result:call.result, usage:call.usage}, null, 2)) + "</pre></details>" +
        (detail ? "<p>" + esc(detail) + "</p>" : "") + "</article>";
    }).join("");
    updateDisclosureHTML(this.root, html, this.session);
  }
}
