"""Browser fetch deadlines include stalled bodies and preserve HTTP failures."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_fetch_deadline_and_cleanup():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for console request tests")
    source = (Path(__file__).parents[1] / "egoassist/console/request.js").read_text(encoding="utf-8")
    program = source + r'''
const assert = require('node:assert/strict');
function stalled(signal) {
  return new Promise((resolve, reject) => {
    if (signal.aborted) reject(signal.reason);
    else signal.addEventListener('abort', () => reject(signal.reason), {once: true});
  });
}
(async () => {
  global.fetch = async (_, options) => ({ok: true, text: () => stalled(options.signal)});
  await assert.rejects(fetchJSON('/body-stalled', {timeoutMs: 20}), /body-stalled/);
  global.fetch = (_, options) => stalled(options.signal);
  await assert.rejects(fetchJSON('/headers-stalled', {timeoutMs: 20}), /headers-stalled/);
  const cancelled = new AbortController();
  cancelled.abort();
  await assert.rejects(fetchJSON('/cancelled', {signal: cancelled.signal}), /cancelled/);
  global.fetch = async () => ({ok: false, status: 401, text: async () => '{"message":"expired"}'});
  await assert.rejects(fetchJSON('/auth'), error => error.status === 401 && error.message === 'expired');
  global.fetch = async () => { throw new TypeError('Failed to fetch'); };
  await assert.rejects(fetchJSON('/offline'), error => error.connectionUncertain && error.message.includes('暂时无法连接') && !error.message.includes('Failed to fetch'));
  global.fetch = async () => ({ok: true, text: async () => '{"jobId":"retained-job"}'});
  assert.deepEqual(await fetchJSON('/jobs'), {jobId: 'retained-job'});
  global.fetch = async () => ({ok: true, text: async () => 'invalid json'});
  await assert.rejects(fetchJSON('/invalid'), /JSON/);
})().catch(error => { console.error(error); process.exitCode = 1; });
'''
    subprocess.run([node, "-"], input=program, text=True, encoding="utf-8", check=True, timeout=10)
