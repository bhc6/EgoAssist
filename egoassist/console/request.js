// Bound both headers and body reads; aborting a fetch does not cancel a durable server job.
async function fetchJSON(path, options = {}) {
  const {timeoutMs = 15000, signal, ...fetchOptions} = options;
  const controller = new AbortController();
  const abort = () => controller.abort(signal.reason);
  if (signal?.aborted) abort();
  else signal?.addEventListener("abort", abort, {once: true});
  const timer = setTimeout(() => controller.abort(new DOMException("请求超时", "TimeoutError")), timeoutMs);
  try {
    const response = await fetch(path, {...fetchOptions, signal: controller.signal});
    const body = await response.text();
    let result;
    try { result = JSON.parse(body); }
    catch (_) { throw Error((response.ok ? "服务返回了无效 JSON：" : "请求失败：") + body.slice(0, 180)); }
    if (!response.ok) {
      const error = Error(result.message || result.error || "请求失败");
      error.status = response.status;
      throw error;
    }
    return result;
  } catch (error) {
    if (controller.signal.aborted) {
      const timeout = controller.signal.reason?.name === "TimeoutError";
      const failure = Error((timeout ? "请求超时" : "请求已取消") + "：" + path + "。已受理的后台任务可在恢复连接后查看。");
      failure.connectionUncertain = timeout;
      throw failure;
    }
    if (error instanceof TypeError) {
      const failure = Error("暂时无法连接本地服务，正在等待恢复。操作是否生效需恢复后核对。");
      failure.connectionUncertain = true;
      throw failure;
    }
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", abort);
  }
}
