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
            acquired = time.perf_counter()
            self._load()
            loaded = time.perf_counter()
            segments = self.diarizer.process(samples).sort_by_start_time()
            segmented = time.perf_counter()
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
                "timingMs": {"queue": round((acquired - started) * 1000, 1),
                             "load": round((loaded - acquired) * 1000, 1),
                             "diarization": round((segmented - loaded) * 1000, 1),
                             "profileEmbedding": round((time.perf_counter() - segmented) * 1000, 1)},
                "model": "sherpa-onnx/pyannote-3.0+3dspeaker-campplus-zh"}

    def warmup(self):
        started = time.perf_counter()
        if not self.available():
            return {"status": "unavailable", "reason": "speaker-models-missing"}
        with self.lock:
            self._load()
        return {"status": "ready", "latencyMs": round((time.perf_counter() - started) * 1000, 1)}


MODELS = SpeakerModels()


class SpeakerRegistry:
    def __init__(self, journal):
        self.journal = journal
        self.profiles = {}
        self.merges = []
        self.contaminated = set()
        for row in journal.entries(types={'speaker.profile', 'speaker.merge', 'speaker.merge.undo', 'scene.turn'}):
            if row.get("type") == "speaker.profile":
                self.profiles[row["speakerId"]] = dict(row)
            elif row.get('type') == 'speaker.merge':
                self.merges.append(row)
            elif row.get('type') == 'speaker.merge.undo':
                self.merges = [m for m in self.merges if m['id'] != row['mergeId']]
            elif row.get('type') == 'scene.turn':
                segments = row.get('speaker', {}).get('segments', [])
                for segment in segments:
                    if segment.get('method') == 'new-acoustic-profile' and any(
                        other['cluster'] != segment['cluster'] and max(other['startMs'], segment['startMs']) < min(other['endMs'], segment['endMs'])
                        for other in segments):
                        self.contaminated.add(segment['speakerId'])

    def canonical(self, speaker_id):
        links = {m['sourceId']: m['targetId'] for m in self.merges}
        seen = set()
        while speaker_id in links:
            if speaker_id in seen:
                raise ValueError('speaker merge cycle')
            seen.add(speaker_id)
            speaker_id = links[speaker_id]
        return speaker_id

    def merge(self, source_id, target_id):
        if source_id not in self.profiles or target_id not in self.profiles:
            raise ValueError('请选择有效的声音档案')
        if source_id != self.canonical(source_id) or target_id != self.canonical(target_id) or source_id == target_id:
            raise ValueError('请选择两个不同的独立声音档案')
        self.merges.append(self.journal.append('speaker.merge', {'sourceId': source_id, 'targetId': target_id,
            'identitySource': 'account-confirmed', 'meaning': '保留目标称呼和身份，原始语音与声纹不删除'}))

    def undo_merge(self, merge_id):
        if not self.merges or self.merges[-1]['id'] != merge_id:
            raise ValueError('请从最近一次合并开始撤销')
        self.journal.append('speaker.merge.undo', {'mergeId': merge_id})
        self.merges.pop()

    def public(self):
        # Journal insertion order is stable across renames and restarts, including
        # old profiles which predate readable labels. IDs remain audit keys only.
        labels = {sid: row.get('name') or f'声音 {index}' for index, (sid, row) in enumerate(self.profiles.items(), 1)}
        result = []
        for sid, row in self.profiles.items():
            if self.canonical(sid) != sid:
                continue
            members = [key for key in self.profiles if self.canonical(key) == sid]
            result.append({**{k: v for k, v in row.items() if k != 'embedding'}, 'displayName': labels[sid],
                           'memberIds': members, 'memberNames': [labels[key] for key in members],
                           'matchingEligible': any(key not in self.contaminated for key in members),
                           'excludedSampleCount': sum(key in self.contaminated for key in members)})
        return result

    def merge_state(self):
        suggestions = []
        rows = self.public()[:100]
        for index, left in enumerate(rows):
            for right in rows[index + 1:]:
                score = max(float(np.dot(self.profiles[a]['embedding'], self.profiles[b]['embedding']))
                            for a in left['memberIds'] for b in right['memberIds'])
                if score >= .75:
                    suggestions.append({'sourceId': left['speakerId'], 'targetId': right['speakerId'],
                                        'similarity': round(score, 4), 'claim': 'similarity-not-identity'})
        return {'suggestions': sorted(suggestions, key=lambda s: -s['similarity'])[:12],
                'latestMerge': self.merges[-1] if self.merges else None,
                'suggestionScope': 'first-100-active-profiles'}

    def match(self, analysis):
        result = {**analysis, "segments": []}
        if not analysis.get("segments"):
            return {**result, "status": analysis.get("status") if analysis.get("status") in {"unknown", "unavailable"} else "unknown",
                    "speakerId": None, "wearer": False, "name": None}
        matched_clusters = {}
        threshold = float(os.getenv("EGOASSIST_SPEAKER_MATCH_THRESHOLD", "0.65"))
        for segment in analysis.get("segments", []):
            # A mono crop containing simultaneous clusters is a mixture, not an
            # isolated person's voice. It must not enroll or identify anyone.
            if any(other["cluster"] != segment["cluster"]
                   and max(other["startMs"], segment["startMs"]) < min(other["endMs"], segment["endMs"])
                   for other in analysis["segments"]):
                result["segments"].append({k: v for k, v in segment.items() if k != "embedding"} | {
                    "speakerId": None, "name": None, "wearer": False, "method": "overlapping-speech",
                    "reason": "Mono overlap cannot provide an isolated speaker embedding",
                    "claim": "Acoustic hypothesis; not authentication or proof of personal identity"})
                continue
            vector = segment["embedding"]
            occupied = {sid for cluster, sid in matched_clusters.items() if cluster != segment["cluster"]}
            grouped = {}
            for sid, profile in self.profiles.items():
                if sid in self.contaminated:
                    continue
                root = self.canonical(sid)
                grouped[root] = max(grouped.get(root, -1), float(np.dot(vector, profile['embedding'])))
            all_scores = sorted(((score, sid) for sid, score in grouped.items()), reverse=True)
            scores = [score for score in all_scores if score[1] not in occupied]
            best = scores[0] if scores else (0, None)
            margin = best[0] - (scores[1][0] if len(scores) > 1 else 0)
            speaker_id = matched_clusters.get(segment["cluster"])
            method = "within-clip-cluster" if speaker_id else "unknown"
            conflict = any(sid in occupied and score >= threshold - 0.15 and score >= best[0] - 0.08
                           for score, sid in all_scores)
            if not speaker_id and conflict:
                method = "cluster-conflict"
            elif not speaker_id and best[0] >= threshold and margin >= 0.08:
                speaker_id, method = best[1], "acoustic-match"
            elif not speaker_id and any(sid in occupied and score >= threshold - 0.15 for score, sid in all_scores):
                method = "cluster-conflict"
            elif not speaker_id and (not all_scores or all_scores[0][0] < threshold - 0.15):
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
        speaker_id = self.canonical(speaker_id)
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
