from egoassist.realtime.context_harness import ContextHarness, SessionJournal
from egoassist.realtime.routed_tools import execute_reference_request


def test_unavailable_reference_never_executes_and_records_failed_receipt(tmp_path):
    harness = ContextHarness(SessionJournal(tmp_path / "journal.jsonl"))
    calls = []
    result = execute_reference_request(harness, lambda *args: calls.append(args), [], {"turnId": "t1"})
    assert result["status"] == "unsupported"
    assert not calls
    entries = harness.journal.entries()
    assert entries[-1]["type"] == "tool_result" and entries[-1]["isError"]
    assert entries[-2]["callId"] == entries[-1]["callId"]


def test_reference_failure_never_becomes_success(tmp_path):
    harness = ContextHarness(SessionJournal(tmp_path / "journal.jsonl"))
    def stale(*args):
        raise RuntimeError("tool call belongs to a stale case connection")
    result = execute_reference_request(harness, stale, [{"name": "show_reference"}], {"turnId": "t2"})
    assert result["status"] == "failed"
    assert harness.journal.entries()[-1]["isError"]
