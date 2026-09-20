"""Freeze all 500 official JAKE questions and seven days of source-only history."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from egoassist.eval.controlled_retrieval import write_json
from compare_egolife_mcq import fingerprint, read
from egolife_independent_modes import REVISION, REPO, get_bytes, public_question, sha, source_records, tree


def freeze_questions(official):
    if len(official) != 500 or len({r["ID"] for r in official}) != 500:
        raise ValueError("Expected exactly 500 unique official JAKE questions")
    questions = [public_question(row) for row in official]
    private = [{"id": r["ID"], "answer": r["answer"], "target": r["target_time"], "type": r["type"],
                "needAudio": r["need_audio"], "needName": r["need_name"], "lastTime": r["last_time"]}
               for r in official]
    for query, gold in zip(questions, private):
        if set(query["options"]) != set("ABCD") or any(not isinstance(v, str) or not v.strip() for v in query["options"].values()):
            raise ValueError("Invalid question options")
        if str(gold["answer"]).strip().upper() not in "ABCD" or len(str(gold["answer"]).strip()) != 1:
            raise ValueError("Invalid gold choice")
    return questions, private


def prepare(args):
    args.output.mkdir(parents=True, exist_ok=False)
    old_freeze = read(args.previous / "freeze.json")
    for name, expected in old_freeze["files"].items():
        if fingerprint(args.previous / name) != expected:
            raise ValueError("Prior frozen dataset changed: " + name)
    official_file = args.previous / "official-qa-private.json"
    expected_qa = read(args.previous / "selection.json")["officialQaSha256"]
    if fingerprint(official_file) != expected_qa:
        raise ValueError("Official QA snapshot changed")
    official = read(official_file)
    questions, private = freeze_questions(official)
    prior_questions = read(args.previous / "questions.json")
    protocol = {"schema": "egolife.jake.full500.v1", "repository": REPO, "revision": REVISION,
                "createdAt": datetime.now(timezone.utc).isoformat(), "questionCount": 500,
                "selection": "all official JAKE questions, no category/day/target-format exclusions",
                "corpusDays": [f"DAY{i}" for i in range(1, 8)], "participants": 1,
                "inputScope": "official DenseCaption/Transcript SRT; not raw video perception",
                "queryClock": "HHMMSSxx floored to whole seconds, matching existing adapter; not claimed paper-equivalent",
                "eligibility": "source end <= query cutoff, plus common production hard constraints",
                "goldUse": "post-prediction evaluation only; targets retained verbatim, never input to retrieval",
                "independentTest": False, "priorExposedOfficialIds": [q["id"] for q in prior_questions],
                "priorExposure": "30 exact official questions and earlier DAY1 development; remaining overlap not audited",
                "sourceSnapshot": str(args.previous.resolve()), "officialQaSha256": expected_qa,
                "answerModel": None, "paperAlignment": "dataset coverage only; model and execution protocol pending",
                "cost": None, "codeSha256": fingerprint(Path(__file__))}
    write_json(args.output / "protocol.json", protocol)
    write_json(args.output / "questions.json", questions)
    write_json(args.output / "gold-private.json", private)
    sources = read(args.previous / "sources.json")
    manifest = [{**r, "localFile": str((args.previous / "source-files" / r["path"]).resolve())}
                for r in read(args.previous / "source-manifest.json")]
    for row in manifest:
        if fingerprint(row["localFile"]) != row["sha256"]:
            raise ValueError("Cached source file changed")
    paths = []
    for kind in ("DenseCaption", "Transcript"):
        paths.extend(r["path"] for r in tree(f"EgoLifeCap/{kind}/A1_JAKE/DAY1") if r["path"].endswith(".srt"))

    def download(path):
        data = get_bytes(path)
        local = args.output / "source-files" / path
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(data)
        return path, data, local

    with ThreadPoolExecutor(max_workers=4) as pool:
        for index, (path, data, local) in enumerate(pool.map(download, sorted(paths)), 1):
            sources.extend(source_records(path, data))
            manifest.append({"path": path, "sha256": sha(data), "bytes": len(data), "localFile": str(local.resolve())})
            print(f"DAY1 source files {index}/{len(paths)}", flush=True)
    if len({s["sourceId"] for s in sources}) != len(sources):
        raise ValueError("Duplicate source identifiers")
    if {s["day"] for s in sources} != set(protocol["corpusDays"]):
        raise ValueError("Incomplete day coverage")
    write_json(args.output / "sources.json", sources)
    write_json(args.output / "source-manifest.json", manifest)
    summary = {"questions": len(questions), "categories": dict(Counter(g["type"] for g in private)),
               "queryDays": dict(Counter(q["day"] for q in questions)), "sources": len(sources),
               "sourceDays": dict(Counter(s["day"] for s in sources)), "sourceKinds": dict(Counter(s["kind"] for s in sources)),
               "sourceFiles": len(manifest), "excludedQuestions": 0, "modelCalls": 0,
               "targetAnnotations": "verbatim, including lists and malformed clocks; never filter QA by target parseability"}
    write_json(args.output / "selection.json", summary)
    names = ("protocol.json", "questions.json", "gold-private.json", "sources.json", "source-manifest.json", "selection.json")
    write_json(args.output / "freeze.json", {"frozenAt": datetime.now(timezone.utc).isoformat(),
               "files": {name: fingerprint(args.output / name) for name in names}, "questions": 500,
               "records": len(sources), "participants": 1, "independentTest": False})
    print(summary, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--previous", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    prepare(parser.parse_args())
