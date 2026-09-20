"""Audit and replay actual stored microphone audio with the local speaker model.

Reads the source session without modifying it. Similarity is not identity truth.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from egoassist._env import load_env
from egoassist.core.journal import SessionJournal
from egoassist.realtime.speakers import MODELS, SpeakerRegistry


def verify(source, output):
    load_env(str(ROOT / ".env"))
    output.mkdir(parents=True, exist_ok=False)
    profiles, turns, revisions = {}, {}, 0
    for line in source.open(encoding="utf-8"):
        row = json.loads(line)
        if row["type"] == "speaker.profile":
            profiles[row["speakerId"]] = row
        if row["type"] == "scene.turn":
            turns[row["turnId"]] = row
            revisions += 1
    media_root = source.parent / "media" / source.stem
    audio_turns = defaultdict(list)
    for tid, row in turns.items():
        if row.get("audio"):
            audio_turns[row["audio"]["sha256"]].append(tid)
    report = {"source": str(source.resolve()), "realLocalModel": True, "identityGroundTruth": False,
              "originalProfiles": len(profiles), "uniqueTurns": len(turns), "sceneRevisions": revisions,
              "duplicateAudioTurns": [v for v in audio_turns.values() if len(v) > 1],
              "similarProfilePairs": [], "replayed": [], "passed": False}
    for index, (sid, profile) in enumerate(profiles.items()):
        for other_id, other in list(profiles.items())[index + 1:]:
            similarity = float(np.dot(profile["embedding"], other["embedding"]))
            if similarity >= .75:
                report["similarProfilePairs"].append({"ids": [sid, other_id], "cosineSimilarity": round(similarity, 4)})
    registry = SpeakerRegistry(SessionJournal(output / "replay.jsonl", session_id="speaker-replay"))
    try:
        assert MODELS.available(), "local acoustic models unavailable"
        for tid, row in turns.items():
            ref = row.get("audio")
            if not ref:
                continue
            path = media_root / ref["assetId"]
            assert path.resolve().parent == media_root.resolve()
            assert hashlib.sha256(path.read_bytes()).hexdigest() == ref["sha256"]
            with wave.open(str(path)) as wav:
                assert (wav.getframerate(), wav.getnchannels(), wav.getsampwidth()) == (16000, 1, 2)
                pcm = wav.readframes(wav.getnframes())
            analysis = MODELS.analyze(pcm)
            result = registry.match(analysis)
            # Do not write acoustic embeddings to the human-facing report.
            report["replayed"].append({"turnId": tid, "audioSha256": ref["sha256"], "result": result})
            assert all(s["speakerId"] is None for s in result["segments"] if s["method"] == "overlapping-speech")
        report["replayedProfiles"] = [{"speakerId": p["speakerId"], "displayName": p["displayName"]} for p in registry.public()]
        report["methods"] = dict(Counter(s["method"] for r in report["replayed"] for s in r["result"]["segments"]))
        report["passed"] = True
    except Exception as exc:
        report["error"] = type(exc).__name__ + ": " + str(exc)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in {"replayed", "replayedProfiles"}}, ensure_ascii=True))
    print("replayed audio:", len(report["replayed"]), "profiles:", len(registry.profiles))
    return report["passed"]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    raise SystemExit(0 if verify(args.journal, args.output) else 1)
