"""Run an existing acceptance script with a unique directory and durable provenance."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from uuid import uuid4

from audit_outputs import ROOT, code_provenance, write_manifest


def run(script, name, arguments, timeout):
    if Path(script).name != script or not (ROOT / "scripts" / script).is_file():
        raise ValueError("Choose an existing filename under scripts/")
    if not script.endswith(".py") or script in {"run_experiment.py", "audit_outputs.py"}:
        raise ValueError("Choose a Python experiment that accepts --output")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError("Name must contain 1-64 letters, digits, underscores or hyphens")
    if any(value == "--output" or value.startswith("--output=") for value in arguments):
        raise ValueError("The runner owns --output; choose --name instead")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = ROOT / "outputs" / "runs" / f"{stamp}-{name}-{uuid4().hex[:8]}"
    root.mkdir(parents=True, exist_ok=False)
    command = [sys.executable, str(ROOT / "scripts" / script), "--output", str(root / "artifacts"), *arguments]
    record = {"schema": "egoassist.experiment.v1", "startedAt": datetime.now(timezone.utc).isoformat(),
              "status": "running", "command": command, "timeoutSeconds": timeout,
              "provenance": code_provenance(), "passed": False}
    def persist():
        (root / "run.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    persist()
    print(str(root), flush=True)
    started = time.perf_counter()
    try:
        with (root / "process.log").open("w", encoding="utf-8") as log:
            process = subprocess.run(command, cwd=ROOT, stdout=log, stderr=subprocess.STDOUT, timeout=timeout)
        record.update(returncode=process.returncode, passed=process.returncode == 0,
                      status="completed" if process.returncode == 0 else "failed")
    except (OSError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        record.update(status="interrupted" if isinstance(error, KeyboardInterrupt) else "failed",
                      error=type(error).__name__)
    finally:
        record.update(finishedAt=datetime.now(timezone.utc).isoformat(),
                      elapsedSeconds=round(time.perf_counter() - started, 3))
        persist()
        manifest = write_manifest(root, root / "manifest.json", provenance=record["provenance"])
    print(json.dumps({"output": str(root), "status": record["status"],
                      "structurallyComplete": manifest["structurallyComplete"]}), flush=True)
    return record["passed"] and manifest["structurallyComplete"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--timeout", type=float, default=900)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    raise SystemExit(0 if run(args.script, args.name, arguments, args.timeout) else 1)
