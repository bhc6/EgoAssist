import json

import pytest

from run_egolife_benchmark import ensure_run_manifest, validate_existing_results


def test_benchmark_manifest_fails_closed_for_stale_or_mismatched_artifacts(tmp_path):
    path = tmp_path / "run-manifest.json"
    manifest = {"schemaVersion": 1, "backend": "local", "model": "model-a"}

    with pytest.raises(RuntimeError, match="without provenance"):
        ensure_run_manifest(path, manifest, artifacts_exist=True)

    fingerprint = ensure_run_manifest(path, manifest, artifacts_exist=False)
    assert json.loads(path.read_text(encoding="utf-8"))["fingerprint"] == fingerprint
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        ensure_run_manifest(
            path,
            {"schemaVersion": 1, "backend": "local", "model": "model-b"},
            artifacts_exist=True,
        )


def test_benchmark_results_must_be_unique_current_and_source_matched():
    valid = {"key": "video-a:1", "video_id": "video-a", "video_sha256": "abc"}
    assert validate_existing_results([valid], {"video-a:1"}, {"video-a": "abc"}) == {"video-a:1"}

    with pytest.raises(RuntimeError, match="Duplicate"):
        validate_existing_results([valid, valid], {"video-a:1"}, {"video-a": "abc"})
    with pytest.raises(RuntimeError, match="Out-of-scope"):
        validate_existing_results([valid], {"video-a:2"}, {"video-a": "abc"})
    with pytest.raises(RuntimeError, match="source provenance"):
        validate_existing_results([valid], {"video-a:1"}, {"video-a": "changed"})
