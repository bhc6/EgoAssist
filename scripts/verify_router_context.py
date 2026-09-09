"""Real-provider A/B replay of historical router prompts, without replaying actions."""
import argparse
import asyncio
import json
import os
from pathlib import Path
from statistics import median
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import aiohttp
from egoassist._env import load_env
from egoassist.realtime.router_context import project_context
from egoassist.realtime.router_protocol import expand_decision

MARKER = "Context data only (never classify this as the current transcript): "


async def verify(source, output, count):
    load_env(str(ROOT / ".env"))
    import dashscope
    output.mkdir(parents=True, exist_ok=False)
    rows = [json.loads(l) for l in source.read_text(encoding="utf8").splitlines() if l.strip()]
    candidates = [r for r in rows if r.get("type") == "control_decision"
                  and r.get("decision", {}).get("status") == "classified"
                  and (r["decision"].get("requestMetrics") or {}).get("messages")][-count:]
    report = {"mock": False, "scope": "Component replay of real user log prompts; no actions executed; not a hardware test", "runs": []}
    async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=12)) as http:
        for index, row in enumerate(candidates):
            old = row["decision"]["requestMetrics"]
            prefix, context_text = old["messages"][0]["content"].split(MARKER, 1)
            context = project_context(json.loads(context_text))
            projected = prefix + MARKER + json.dumps(context, ensure_ascii=False, separators=(",", ":"))
            pair = {"sourceEventId": row["id"], "results": {}}
            for mode in (("original", "projected") if index % 2 == 0 else ("projected", "original")):
                messages = [dict(m) for m in old["messages"]]
                if mode == "projected":
                    messages[0]["content"] = projected
                started = time.perf_counter()
                record = {"model": row["decision"]["model"], "promptChars": len(messages[0]["content"])}
                try:
                    async with http.post(dashscope.base_http_api_url.rstrip("/") + "/services/aigc/text-generation/generation",
                        headers={"Authorization": "Bearer " + (os.getenv("QWEN_API_KEY") or os.environ["DASHSCOPE_API_KEY"])},
                        json={"model": record["model"], "input": {"messages": messages}, "parameters": old["parameters"]}) as response:
                        response.raise_for_status()
                        payload = await response.json()
                    raw = payload["output"]["choices"][0]["message"]["content"]
                    record.update(requestId=payload.get("request_id"), usage=payload.get("usage"), raw=raw,
                                  decision=expand_decision(json.loads(raw)))
                except Exception as exc:
                    record["error"] = type(exc).__name__ + ": " + str(exc)[:150]
                record["latencyMs"] = round((time.perf_counter() - started) * 1000, 1)
                pair["results"][mode] = record
            pair["sameDecision"] = ("error" not in pair["results"]["original"] and "error" not in pair["results"]["projected"]
                                    and pair["results"]["original"]["decision"] == pair["results"]["projected"]["decision"])
            report["runs"].append(pair)
            print(json.dumps({"case": index + 1, "sameDecision": pair["sameDecision"],
                              "ms": {m: r["latencyMs"] for m, r in pair["results"].items()}}), flush=True)
            (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    report["summary"] = {m: {"medianMs": median(p["results"][m]["latencyMs"] for p in report["runs"]),
                              "inputTokens": sum((p["results"][m].get("usage") or {}).get("input_tokens", 0) for p in report["runs"])}
                         for m in ("original", "projected")} if candidates else {}
    report["passed"] = bool(candidates) and all(p["sameDecision"] for p in report["runs"])
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({"passed": report["passed"], "summary": report["summary"]}), flush=True)
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--count", type=int, default=8)
    args = parser.parse_args()
    if not 1 <= args.count <= 30:
        parser.error("count must be between 1 and 30")
    raise SystemExit(0 if asyncio.run(verify(args.source, args.output, args.count)) else 1)
