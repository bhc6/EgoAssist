"""Build a local evidence presentation from retained runs, without rerunning models."""
import argparse
from copy import deepcopy
import html
import json
import os
from pathlib import Path
import xml.etree.ElementTree as ET

from audit_outputs import code_provenance, file_hash, write_manifest
from run_story_showcase import render, save


def build(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    reports = [json.loads((root / "report.json").read_text(encoding="utf-8")) for root in args.runs]
    merged = {**deepcopy(reports[0]), "cases": [], "checks": [], "videos": [], "sourceRuns": []}
    links = []
    for root, report in zip(args.runs, reports):
        prefix = Path(os.path.relpath(root.resolve(), output)).as_posix()
        merged["sourceRuns"].append({"path": str(root.resolve()), "sha256": file_hash(root / "report.json"), "passed": report["passed"]})
        for case in deepcopy(report["cases"]):
            for step in case["steps"]:
                step["audio"] = [prefix + "/" + p for p in step.get("audio", [])]
            case["screenshots"] = [prefix + "/" + p for p in case.get("screenshots", [])]
            merged["cases"].append(case)
            links.append(f'<li>{html.escape(case["title"])}：<a href="{prefix}/journals/{case["id"]}.json">原始链路日志</a> · <a href="{prefix}/report.json">本次结果</a></li>')
        merged["checks"].extend(report["checks"])
        merged["videos"].extend(prefix + "/" + p for p in report.get("videos", []))
    cross = json.loads((args.cross_lane / "report.json").read_text(encoding="utf-8"))
    prefix = Path(os.path.relpath(args.cross_lane.resolve(), output)).as_posix()
    merged["crossLane"] = {"passed": cross["passed"], "question": cross["comparison"]["question"],
                           "comparisonWallMs": cross["comparisonWallMs"], "sourceReport": str(args.cross_lane / "report.json")}
    merged["tests"] = []
    for path in args.tests:
        suite = ET.parse(path).getroot().find("testsuite")
        merged["tests"].append({"path": str(path), **suite.attrib})
    merged["passed"] = all(r["passed"] for r in reports) and cross["passed"]
    save(output / "report.json", merged)
    render(merged, output)
    page = (output / "index.html").read_text(encoding="utf-8")
    extra = f'<section><h2>主动澄清的真实案例</h2><p>真实视频画面与旧记忆冲突，系统主动问：</p><p class="answer">{html.escape(cross["comparison"]["question"])}</p><p>用户明确纠正后旧卡被替换，刷新不重复提问。现场比对 {cross["comparisonWallMs"]} ms，不代表完整语音往返时间。</p><a href="{prefix}/report.json">完整闭环日志</a><img loading="lazy" src="{prefix}/review-1440.png" alt="主动澄清真实控制台"></section>'
    extra += '<section><h2>逐场景原始证据</h2><ul>' + "".join(links) + '</ul><p><a href="manifest.json">文件哈希清单</a></p></section>'
    extra += '<section><h2>失败记录与演示条件</h2><p>初次测试发现文字纠正未写入、任务暂停误路由、跨会话关系名称不匹配，已修复并复验。首次真实麦克风与文字混合输入受到环境谈话影响，回答偏离提问；现场提供独立麦克风开关，摄像头文字演示时关闭收音。此开关不证明多人混合对话已解决。</p><p>真实摄像头后续被遮挡，系统返回看不清。此项证明当前画面处理和不确定性表达，不证明正常室内识别已在该轮通过。设备早期非空白画面见设备检查截图。</p><p>历史输出目录完整保留：story-showcase-20260915-r1/r2/r3 与 story-handoff-20260915-r1/r2/r3。handoff-r2 的自动检查曾报告通过，但人工检查发现答非所问，不计入最终语义通过证据。</p></section>'
    page = page.replace("</main>", extra + "</main>")
    (output / "index.html").write_text(page, encoding="utf-8")
    manifest = write_manifest(output, output / "manifest.json", provenance=code_provenance())
    print(json.dumps({"passed": merged["passed"], "checks": len(merged["checks"]), "manifest": manifest["structurallyComplete"], "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", nargs="+", type=Path, required=True)
    parser.add_argument("--cross-lane", type=Path, required=True)
    parser.add_argument("--tests", nargs="+", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args())
