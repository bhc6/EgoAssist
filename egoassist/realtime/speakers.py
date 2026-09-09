"""Local acoustic segmentation and evidence-based, case-scoped speaker matching."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from uuid import uuid4

import numpy as np


class SpeakerModels:
    def __init__(self):
        self.lock = threading.Lock()
        self.diarizer = self.extractor = None
        self.root = Path(os.getenv("EGOASSIST_SPEAKER_MODELS", str(Path(__file__).resolve().parents[2] / ".cache/shared-perception")))

    def available(self):
        return all((self.root / name).is_file() for name in ("speaker.onnx", "segmentation.onnx"))

    def _load(self):
        if self.diarizer is not None:
            return
        import sherpa_onnx as sherpa
        embedding = sherpa.SpeakerEmbeddingExtractorConfig(model=str(self.root / "speaker.onnx"), num_threads=2)
        config = sherpa.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(self.root / "segmentation.onnx")), num_threads=2),
            embedding=embedding, clustering=sherpa.FastClusteringConfig(threshold=0.5))
        self.diarizer = sherpa.OfflineSpeakerDiarization(config)
        self.extractor = sherpa.SpeakerEmbeddingExtractor(embedding)

    def analyze(self, pcm: bytes):
        started = time.perf_counter()
        if not self.available():
            return {"status": "unavailable", "reason": "speaker-models-missing", "segments": []}
        samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        if len(samples) < 12800:
            return {"status": "unknown", "reason": "audio-too-short", "segments": []}
        with self.lock:
            self._load()
            segments = self.diarizer.process(samples).sort_by_start_time()
            rows = []
            omitted = 0
            for segment in segments:
                clip = samples[int(segment.start * 16000):int(segment.end * 16000)]
                if len(clip) < 12800:
                    omitted += 1
                    continue
                stream = self.extractor.create_stream()
                stream.accept_waveform(16000, clip)
                stream.input_finished()
                if not self.extractor.is_ready(stream):
                    omitted += 1
                    continue
                vector = np.asarray(self.extractor.compute(stream), dtype=np.float32)
                norm = np.linalg.norm(vector)
                if not np.isfinite(norm) or norm <= 0:
                    omitted += 1
                    continue
                rows.append({"startMs": round(segment.start * 1000), "endMs": round(segment.end * 1000),
                             "cluster": segment.speaker, "embedding": (vector / norm).tolist()})
        return {"status": "analyzed", "segments": rows, "omittedSegments": omitted,
                "clusterCount": len({s.speaker for s in segments}), "latencyMs": round((time.perf_counter() - started) * 1000, 1),
                "model": "sherpa-onnx/pyannote-3.0+3dspeaker-campplus-zh"}


MODELS = SpeakerModels()


class SpeakerRegistry:
    def __init__(self, journal):
        self.journal = journal
        self.profiles = {}
        for row in journal.entries():
            if row.get("type") == "speaker.profile":
                self.profiles[row["speakerId"]] = dict(row)

    def public(self):
        return [{k: v for k, v in row.items() if k not in {"embedding"}} for row in self.profiles.values()]

    def match(self, analysis):
        result = {**analysis, "segments": []}
        matched_clusters = {}
        threshold = float(os.getenv("EGOASSIST_SPEAKER_MATCH_THRESHOLD", "0.65"))
        for segment in analysis.get("segments", []):
            vector = segment["embedding"]
            occupied = {sid for cluster, sid in matched_clusters.items() if cluster != segment["cluster"]}
            scores = sorted(((float(np.dot(vector, p["embedding"])), sid) for sid, p in self.profiles.items()
                             if sid not in occupied), reverse=True)
            best = scores[0] if scores else (0, None)
            margin = best[0] - (scores[1][0] if len(scores) > 1 else 0)
            speaker_id = matched_clusters.get(segment["cluster"])
            method = "within-clip-cluster" if speaker_id else "unknown"
            if not speaker_id and best[0] >= threshold and margin >= 0.08:
                speaker_id, method = best[1], "acoustic-match"
            elif not speaker_id and (not scores or best[0] < threshold - 0.15):
                speaker_id, method = "speaker-" + uuid4().hex[:12], "new-acoustic-profile"
                self.profiles[speaker_id] = self.journal.append("speaker.profile", {
                    "speakerId": speaker_id, "embedding": vector, "name": None, "wearer": False,
                    "identitySource": "unknown", "model": analysis.get("model")})
            if speaker_id:
                matched_clusters[segment["cluster"]] = speaker_id
            profile = self.profiles.get(speaker_id, {})
            result["segments"].append({k: v for k, v in segment.items() if k != "embedding"} | {
                "speakerId": speaker_id, "name": profile.get("name"), "wearer": profile.get("wearer", False),
                "similarity": round(best[0], 4), "margin": round(margin, 4), "method": method,
                "claim": "Acoustic hypothesis; not authentication or proof of personal identity"})
        ids = {s["speakerId"] for s in result["segments"]}
        complete = not analysis.get("omittedSegments") and analysis.get("clusterCount", 1) == 1
        result["speakerId"] = next(iter(ids)) if complete and len(ids) == 1 and None not in ids else None
        result["status"] = "single-speaker" if result["speakerId"] else "mixed-or-unknown"
        profile = self.profiles.get(result["speakerId"], {})
        result["wearer"] = bool(profile.get("wearer"))
        result["name"] = profile.get("name")
        return result

    def confirm(self, speaker_id, *, name, wearer=False, person_ref=None):
        if speaker_id not in self.profiles:
            raise ValueError("unknown speaker")
        if not isinstance(name, str) or not name.strip() or len(name) > 80 or type(wearer) is not bool:
            raise ValueError("invalid speaker identity")
        if wearer:
            for sid, row in list(self.profiles.items()):
                if sid != speaker_id and row.get("wearer"):
                    self._save(sid, wearer=False)
        self._save(speaker_id, name=name.strip(), wearer=wearer, personRef=person_ref,
                   identitySource="account-confirmed")

    def _save(self, speaker_id, **changes):
        row = {k: v for k, v in self.profiles[speaker_id].items() if k not in {"id", "type", "timestamp", "parentId"}}
        self.profiles[speaker_id] = self.journal.append("speaker.profile", {**row, **changes})
