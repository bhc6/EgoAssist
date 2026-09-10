"""Evidence inventory must preserve corrupt sources and report failed runs."""
import json

import pytest

from scripts.audit_outputs import write_manifest


def test_manifest_preserves_corruption_failed_results_and_hashes(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    journal = source / "events.jsonl"
    content = b'{"type":"session","id":"case"}\n{"type":"message","id":"one","parentId":"missing"}\n{broken\n'
    journal.write_bytes(content)
    (source / "acceptance.json").write_text('{"passed": false, "error": "provider timeout"}')
    manifest = tmp_path / "manifest.json"
    report = write_manifest(source, manifest)
    assert not report["structurallyComplete"]
    assert journal.read_bytes() == content
    rows = {item["path"]: item for item in report["files"]}
    assert rows["events.jsonl"]["invalidLines"] == [3]
    assert rows["events.jsonl"]["brokenParentLines"] == [2]
    assert rows["acceptance.json"]["reportedPassed"] is False
    assert len(rows["events.jsonl"]["sha256"]) == 64
    assert "provider timeout" not in manifest.read_text(encoding="utf-8")
    with pytest.raises(FileExistsError):
        write_manifest(source, manifest)


def test_manifest_does_not_claim_historical_code_provenance(tmp_path):
    source = tmp_path / "run"
    source.mkdir()
    report = write_manifest(source, source / "manifest.json")
    assert report["provenance"] is None
    assert report["structurallyComplete"]
    assert report["fileCount"] == 0
    assert json.loads((source / "manifest.json").read_text())["files"] == []
