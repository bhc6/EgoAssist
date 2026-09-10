"""Tool audit rendering survives stale events and keeps sessions separate."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_tool_rows_preserve_receipts_and_clear_on_session_change():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for console tests")
    source = (Path(__file__).parents[1] / "egoassist/console/tool_activity.js").read_text(encoding="utf-8")
    program = source + r'''
const assert = require('node:assert/strict');
const esc = value => String(value ?? '').replaceAll('<', '&lt;');
const updateDisclosureHTML = (root, html) => { root.html = html; };
const root = {};
const view = new ToolActivityView(root);
view.snapshot('one', [{callId:'1', tool:'actual-model', status:'selection-finished',
  request:{messages:[{content:'<unsafe>'}]}, result:{content:'clarify'}, latencyMs:123}]);
view.event('one', {callId:'1', status:'selecting'});
assert.equal(view.calls.get('1').status, 'selection-finished');
assert.ok(root.html.includes('123 ms'));
assert.ok(root.html.includes('&lt;unsafe>'));
view.event('one', {callId:'1', status:'failed'});
assert.equal(view.calls.get('1').status, 'failed');
assert.ok(view.calls.get('1').result);
view.snapshot('one', [{callId:'2', tool:'action', status:'completed', result:{status:'completed'}}]);
assert.equal(view.calls.size, 2);
view.snapshot('one', [{callId:'2', tool:'action', status:'running'}]);
assert.equal(view.calls.get('2').status, 'completed');
view.snapshot('two', []);
assert.equal(view.calls.size, 0);
assert.equal(root.hidden, true);
'''
    subprocess.run([node, "-"], input=program, text=True, encoding="utf-8", check=True, timeout=10)
