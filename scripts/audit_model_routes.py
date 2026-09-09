"""Summarize actual journal calls, token usage, timing and duplicate image work."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
from statistics import median


def audit(roots):
    routes = defaultdict(list)
    requests = Counter()
    overlaps = []
    controls = []
    files = sorted({p.resolve() for root in roots for p in root.rglob("*.jsonl")})
    sessions = 0
    for path in files:
        rows = [json.loads(line) for line in path.read_text(encoding="utf8").splitlines() if line.strip()]
        if not any(r.get("type") == "session" for r in rows):
            continue
        sessions += 1
        seen = set()
        background = {r["frame"]["frameId"] for r in rows if r.get("type") == "scene.request"}
        decisions = {r.get("turnId"): r.get("decision", {}) for r in rows if r.get("type") == "control_decision"}
        for row in rows:
            kind = row.get("type")
            if kind in {"scene.request", "scene.question.request", "scene.transcription.request", "answer.request", "insight.request"}:
                requests[kind] += 1
            if kind == "scene.question.request":
                frame_id = row["frame"]["frameId"]
                if frame_id in background:
                    overlaps.append({"session": path.stem, "turnId": row["turnId"], "frameId": frame_id})
                action = (decisions.get(row["turnId"], {}).get("interaction") or {}).get("action")
                if action in {"listen", "pause", "cancel", "uncertain"}:
                    controls.append({"session": path.stem, "turnId": row["turnId"], "action": action})
            records = []
            if kind in {"control_decision", "interaction.classification_retry"}:
                decision = row.get("decision") or {}
                metrics = decision.get("requestMetrics") or {}
                if decision.get("model"):
                    records.append(("turn-router", {**decision, **metrics}))
                if metrics.get("cancellationCheck"):
                    records.append(("cancel-verification", metrics["cancellationCheck"]))
            elif kind in {"scene.result", "scene.question.result", "scene.transcription.result", "answer.result", "insight.result"}:
                records.append((kind, row))
            elif kind == "runtime_event":
                event = row.get("eventType")
                if event == "perception.response.requested":
                    requests[event] += 1
                if event in {"response.finished", "perception.response.finished"}:
                    records.append((event, {**row, **(row.get("payload") or {})}))
            for route, record in records:
                identity = record.get("requestId") or record.get("providerRequestId") or record.get("responseId") or row.get("id")
                key = (route, identity)
                if key not in seen:
                    seen.add(key)
                    routes[route].append(record)
    summary = {}
    for name, values in sorted(routes.items()):
        timings = [r["latencyMs"] for r in values if isinstance(r.get("latencyMs"), (int, float))]
        usage = [r["usage"] for r in values if isinstance(r.get("usage"), dict)]
        summary[name] = {"recordedCalls": len(values), "models": dict(Counter(r.get("model") or "unrecorded" for r in values)),
                         "status": dict(Counter(r.get("status") or r.get("providerStatus") or "unrecorded" for r in values)),
                         "timedCalls": len(timings), "medianMs": round(median(timings), 1) if timings else None,
                         "maxMs": max(timings) if timings else None, "usageRecordedCalls": len(usage),
                         "inputTokens": sum(r.get("input_tokens", r.get("inputTokens", 0)) for r in usage),
                         "outputTokens": sum(r.get("output_tokens", r.get("outputTokens", 0)) for r in usage)}
    return {"sessions": sessions, "roots": [str(p) for p in roots], "routes": summary, "requests": dict(requests),
            "sameFrameSceneAndQuestion": overlaps, "speculativeVisionOnControl": controls,
            "limits": "Recorded calls only, not a billing ledger. Missing usage is unknown. Shared frames can serve different purposes. No dataset identity ground truth is inferred."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("roots", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.roots)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
