"""Validated conversation control and durable output permissions, without phrase matching."""
from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import math
import os


def normalize_interaction(value, context, text):
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"audience", "action", "output", "targetTurnId", "quiet", "evidence"}:
        raise ValueError("invalid interaction fields")
    result = {"audience": "assistant", "action": "reply", "output": "speech", **value}
    if result["audience"] not in {"assistant", "other", "uncertain"}:
        raise ValueError("invalid interaction audience")
    if result["action"] not in {"reply", "listen", "resume", "cancel", "pause", "uncertain"}:
        raise ValueError("invalid interaction action")
    if result["output"] not in {"speech", "text", "silent"}:
        raise ValueError("invalid interaction output")
    evidence = result.get("evidence")
    if evidence is not None and (not isinstance(evidence, str) or not evidence or evidence not in text):
        raise ValueError("interaction evidence must quote current transcript")
    allowed = {str(row.get("turnId")) for row in context.get("answers", [])}
    allowed.update(str((context.get(key) or {}).get("turnId", "")) for key in ("previousTurn", "previousAnswer"))
    target = result.get("targetTurnId")
    if target is not None and (not isinstance(target, str) or not target or target not in allowed):
        raise ValueError("interaction target not supplied")
    if result["action"] in {"cancel", "pause"} and not target:
        result.update(action="uncertain", output="silent")
    quiet = result.get("quiet")
    if quiet is not None:
        if not isinstance(quiet, dict) or set(quiet) - {"mode", "seconds", "allowReplies"}:
            raise ValueError("invalid quiet permission")
        if quiet.get("mode") not in {"set", "clear"} or type(quiet.get("allowReplies", True)) is not bool:
            raise ValueError("invalid quiet mode")
        seconds = quiet.get("seconds")
        if isinstance(seconds, str):
            seconds = float(seconds)
            result["quiet"] = {**quiet, "seconds": seconds}
        if seconds is not None and (type(seconds) not in {int, float} or not math.isfinite(seconds) or not 0 < seconds <= 31536000):
            raise ValueError("invalid quiet duration")
    if result["audience"] != "assistant":
        result.update(action="listen" if result["audience"] == "other" else "uncertain", output="silent")
        result.pop("quiet", None)
        result.pop("targetTurnId", None)
    return result


class InteractionState:
    """Session-local media buffers; only permissions and playback evidence persist."""

    def __init__(self, journal):
        self.journal = journal
        saved = journal.entries(types={"interaction.permission"}, tail=1)
        self.permission = dict(saved[-1]) if saved else {}
        holds = journal.entries(types={"interaction.hold"}, tail=1)
        self.paused = bool(holds and holds[-1].get("paused"))
        self.grants = set()
        self.responses = {}
        self.selected_response = None
        history = journal.entries(types={"interaction.playback"}, tail=1)
        self.last_progress = dict(history[-1]) if history else None
        self.max_samples = int(float(os.getenv("EGOASSIST_PLAYBACK_CACHE_SECONDS", "90")) * 24000)

    def snapshot(self):
        permission = dict(self.permission)
        until = permission.get("until")
        if until and datetime.now(timezone.utc) >= datetime.fromisoformat(until):
            permission["quiet"] = False
        return {"quiet": bool(permission.get("quiet")), "until": until,
                "allowReplies": permission.get("allowReplies", True), "paused": self.paused,
                "sourceTurnId": permission.get("turnId"), "playback": self.playback_context()}

    def apply(self, control, turn_id, *, timestamp=None):
        quiet = control.get("quiet")
        if quiet:
            seconds = quiet.get("seconds")
            start = datetime.fromisoformat(timestamp) if timestamp else datetime.now(timezone.utc)
            until = (start + timedelta(seconds=seconds)).isoformat() if seconds else None
            self.permission = self.journal.append("interaction.permission", {
                "quiet": quiet["mode"] == "set", "until": until,
                "allowReplies": quiet.get("allowReplies", True), "turnId": turn_id})
        self.grants.clear()
        self.release()
        if control["output"] == "silent" and control.get("action") == "listen" and control["audience"] == "assistant" and not quiet:
            self.permission = self.journal.append("interaction.permission", {
                "quiet": True, "until": None, "allowReplies": True, "turnId": turn_id})
        if control["output"] == "speech" and control["audience"] == "assistant":
            state = self.snapshot()
            if not state["quiet"] or state["allowReplies"] or (quiet or {}).get("mode") == "clear":
                self.grants.add(turn_id)

    def allows(self, origin=None, purpose="conversation"):
        if self.paused:
            return False
        origin = origin or {}
        if origin.get("turnId") in self.grants:
            return True
        return not self.snapshot()["quiet"] and purpose != "interaction-silent"

    def pause(self):
        if not self.paused:
            self.journal.append("interaction.hold", {"paused": True})
        self.paused = True
        self.grants.clear()

    def release(self):
        if self.paused:
            self.journal.append("interaction.hold", {"paused": False})
        self.paused = False

    def observe(self, payload):
        response_id = payload.get("responseId")
        if not response_id or payload.get("type") not in {"audio", "assistant_transcript", "response_status"}:
            return
        row = self.responses.setdefault(response_id, {"origin": payload.get("origin") or {}, "pcm": bytearray(),
            "playedSamples": 0, "progressKnown": False, "text": "", "complete": False, "overflow": False})
        if payload.get("origin"):
            row["origin"] = payload["origin"]
        if payload["type"] == "audio":
            try:
                pcm = base64.b64decode(payload.get("audio", ""), validate=True)
            except (ValueError, TypeError):
                row["overflow"] = True
                return
            if len(row["pcm"]) + len(pcm) <= self.max_samples * 2:
                row["pcm"].extend(pcm)
            else:
                row["overflow"] = True
            self.selected_response = response_id
        elif payload["type"] == "assistant_transcript":
            row["text"] = payload.get("text", "") if payload.get("done") else row["text"] + payload.get("text", "")
        elif payload.get("status") in {"completed", "interrupted", "failed"}:
            row["complete"] = True
            row["terminalStatus"] = payload["status"]
        while len(self.responses) > 8:
            self.responses.pop(next(iter(self.responses)))

    def progress(self, payload):
        row = self.responses.get(payload.get("responseId"))
        samples = payload.get("playedSamples")
        if not row or type(samples) is not int or not 0 <= samples <= len(row["pcm"]) // 2:
            return False
        row["playedSamples"] = max(row["playedSamples"], samples)
        row["progressKnown"] = True
        self.selected_response = payload["responseId"]
        self.last_progress = self.journal.append("interaction.playback", {"responseId": payload["responseId"],
            "turnId": row["origin"].get("answerObligation") or row["origin"].get("turnId"), "playedSamples": row["playedSamples"],
            "sampleRate": 24000, "status": payload.get("status", "playing"),
            "generatedSamples": len(row["pcm"]) // 2, "generationComplete": row["complete"],
            "claim": "Device-reported playback position, not proof of hearing or attention"})
        return True

    def playback_context(self):
        row = self.responses.get(self.selected_response)
        if not row:
            return self.last_progress
        return {"responseId": self.selected_response, "turnId": row["origin"].get("answerObligation") or row["origin"].get("turnId"),
                "playedMs": round(row["playedSamples"] / 24, 1), "generatedMs": round(len(row["pcm"]) / 48, 1),
                "generatedText": row["text"][-1200:], "complete": row["complete"]}

    def tail(self, turn_id):
        matches = [(key, row) for key, row in self.responses.items()
                   if (row["origin"].get("answerObligation") or row["origin"].get("turnId")) == turn_id]
        if not matches:
            return None
        key, row = matches[-1]
        if row["overflow"] or not row["pcm"] or not row["progressKnown"]:
            return None
        return key, row, bytes(row["pcm"][row["playedSamples"] * 2:])
