"""Inventory retained artifacts without deleting, repairing or exposing their contents.

Hashes describe files at audit time. They are not proof that every real-world
event was captured, nor a transactionally consistent backup of a running DB.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_provenance():
    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout
    paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
    code = {}
    for name in sorted(set(paths)):
        path = ROOT / name
        if name and path.is_file() and path.suffix in {".py", ".js", ".json", ".toml", ".css", ".html", ".md"}:
            code[name] = file_hash(path)
    packages = {}
    for name in ("aiohttp", "dashscope", "SQLAlchemy", "playwright", "pytest"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = None
    return {
        "capturedAt": datetime.now(timezone.utc).isoformat(),
        "gitCommit": git("rev-parse", "HEAD").decode().strip(),
        "worktreeDirty": bool(git("status", "--porcelain")),
        "codeSha256": code, "python": sys.version, "platform": platform.platform(),
        "packages": packages,
    }


def inspect_file(path, root):
    item = {"path": path.relative_to(root).as_posix()}
    if path.is_symlink():
        return {**item, "error": "symlink-not-followed"}
    try:
        before = path.stat()
        item.update(bytes=before.st_size, sha256=file_hash(path))
        if path.suffix == ".jsonl":
            counts, invalid, records = Counter(), [], 0
            seen_ids, broken_parents, duplicates = set(), [], []
            journal = False
            with path.open("rb") as stream:
                for number, line in enumerate(stream, 1):
                    if not line.strip():
                        continue
                    try:
                        row = json.loads(line)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        invalid.append(number)
                        continue
                    records += 1
                    if not isinstance(row, dict):
                        continue
                    if number == 1:
                        journal = row.get("type") == "session"
                    counts[str(row.get("eventType") or row.get("type") or "untyped")] += 1
                    if journal:
                        if row.get("parentId") and row["parentId"] not in seen_ids:
                            broken_parents.append(number)
                        if row.get("id") in seen_ids:
                            duplicates.append(number)
                        if row.get("id"):
                            seen_ids.add(row["id"])
            item.update(records=records, eventCounts=dict(counts), invalidLines=invalid,
                        brokenParentLines=broken_parents, duplicateIdLines=duplicates)
        elif path.suffix == ".json":
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    item["reportedPassed"] = data.get("passed")
                    item["hasReportedError"] = bool(data.get("error"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                item["error"] = "invalid-json"
        after = path.stat()
        item["changedDuringAudit"] = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    except OSError as error:
        item["error"] = type(error).__name__
    return item


def write_manifest(root, output, *, provenance=None):
    root, output = root.resolve(strict=True), output.resolve()
    if not root.is_dir():
        raise ValueError("Artifact root must be a directory")
    files = [inspect_file(path, root) for path in sorted(root.rglob("*"))
             if path != output and (path.is_file() or path.is_symlink())]
    issues = [item["path"] for item in files if item.get("error") or item.get("invalidLines")
              or item.get("brokenParentLines") or item.get("duplicateIdLines") or item.get("changedDuringAudit")]
    report = {
        "schema": "egoassist.artifact-manifest.v1", "auditedAt": datetime.now(timezone.utc).isoformat(),
        "root": str(root), "fileCount": len(files), "totalBytes": sum(item.get("bytes", 0) for item in files),
        "structurallyComplete": not issues, "issues": issues, "files": files,
        "provenance": provenance,
        "claimBoundary": "Inventory of retained files only. Hashes are measured at audit time. No semantic correctness, capture completeness, live-database snapshot or historical code provenance is inferred.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = write_manifest(args.root, args.output)
    print(json.dumps({key: result[key] for key in ("fileCount", "totalBytes", "structurallyComplete", "issues")}))
    raise SystemExit(0 if result["structurallyComplete"] else 1)
