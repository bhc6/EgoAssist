"""Ambient (task-free) observation watchlists and per-case watch sessions.

Task-mode perception binds every frame to an active checkpoint (ADR 005).
Ambient mode observes a small, pre-authorized set of activities when no task is
running, so that a behaviour can be recorded as *not* having happened -- the
evidence an observation-gated reminder needs. See docs/ADR_007.

This module is deliberately domain-free: activity and location labels come from
a reviewed JSON watchlist outside the package, never from code here.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from egoassist.product.contracts import AmbientFrameContext
from egoassist.realtime.task import bounded_text


AMBIENT_SCHEMA_VERSION = "ambient-context.v1"

#: Reserved activity id meaning "the frame was inspected and no watched
#: activity was visible". A first-class value, not a failure code.
NONE_OBSERVED = "none-observed"

#: Reserved location id for an observation whose place could not be determined.
UNKNOWN_LOCATION = "unknown"

_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{1,63}")

_MAX_ACTIVITIES = 16
_MAX_LOCATIONS = 32


def _valid_id(value: str) -> bool:
    return bool(_ID_PATTERN.fullmatch(value))


@dataclass(frozen=True)
class AmbientWatchItem:
    """One watched activity. ``visible_evidence`` is the ambient analogue of
    ``TaskStep.completion``: what must be *visible* for this to count."""

    id: str
    label: str
    visible_evidence: str
    source: str = ""


@dataclass(frozen=True)
class AmbientWatchLocation:
    id: str
    label: str


@dataclass(frozen=True)
class AmbientWatchList:
    """A versioned, checksummed set of activities and locations to watch for.

    Structurally parallel to TaskSpec: reviewable, diffable, and never authored
    by a model. The checksum makes every stored observation traceable to the
    exact watch definition that produced it.
    """

    id: str
    title: str
    version: str
    provenance: str
    checksum: str
    risk: str
    activities: tuple[AmbientWatchItem, ...]
    locations: tuple[AmbientWatchLocation, ...]

    @classmethod
    def load(cls, path: str | Path) -> "AmbientWatchList":
        resolved = Path(path)
        raw = resolved.read_bytes()
        payload = json.loads(raw.decode("utf-8"))
        return cls.from_dict(
            payload,
            provenance=str(payload.get("provenance") or f"file:{resolved.name}"),
            checksum=f"sha256:{hashlib.sha256(raw).hexdigest()}",
        )

    @classmethod
    def from_dict(
        cls,
        payload: dict[str, Any],
        *,
        provenance: str = "api",
        checksum: str | None = None,
    ) -> "AmbientWatchList":
        required = {"id", "title", "version", "activities", "locations"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise ValueError("invalid ambient watchlist specification")
        unknown = set(payload) - {*required, "provenance", "risk"}
        if unknown:
            raise ValueError(f"unknown ambient watchlist field(s): {', '.join(sorted(unknown))}")

        watchlist_id = bounded_text(payload["id"], "watchlist id", 64)
        if not _valid_id(watchlist_id):
            raise ValueError("watchlist id must use 2-64 lowercase letters, digits, hyphens or underscores")
        risk = bounded_text(payload.get("risk", "low"), "watchlist risk", 16)
        if risk not in {"low", "medium", "high"}:
            raise ValueError(f"invalid risk level: {risk}")

        raw_activities = payload["activities"]
        if not isinstance(raw_activities, list) or not 1 <= len(raw_activities) <= _MAX_ACTIVITIES:
            raise ValueError(f"watchlist requires 1-{_MAX_ACTIVITIES} activities")
        activities: list[AmbientWatchItem] = []
        seen_activities: set[str] = set()
        for raw in raw_activities:
            if not isinstance(raw, dict):
                raise ValueError("watchlist activity must be an object")
            extra = set(raw) - {"id", "label", "visible_evidence", "source"}
            if extra:
                raise ValueError(f"unknown watchlist activity field(s): {', '.join(sorted(extra))}")
            missing = {"id", "label", "visible_evidence"} - set(raw)
            if missing:
                raise ValueError(f"watchlist activity missing: {', '.join(sorted(missing))}")
            item_id = bounded_text(raw["id"], "watchlist activity id", 64)
            if not _valid_id(item_id) or item_id in seen_activities:
                raise ValueError(f"invalid or duplicate watchlist activity id: {item_id}")
            if item_id == NONE_OBSERVED:
                raise ValueError(f"watchlist activity id cannot shadow the reserved id {NONE_OBSERVED!r}")
            source = raw.get("source", "")
            if not isinstance(source, str) or len(source) > 300:
                raise ValueError("watchlist activity source must be a string of at most 300 characters")
            activities.append(
                AmbientWatchItem(
                    id=item_id,
                    label=bounded_text(raw["label"], "watchlist activity label", 160),
                    visible_evidence=bounded_text(
                        raw["visible_evidence"], "watchlist activity visible evidence", 600
                    ),
                    source=source.strip(),
                )
            )
            seen_activities.add(item_id)

        raw_locations = payload["locations"]
        if not isinstance(raw_locations, list) or not 1 <= len(raw_locations) <= _MAX_LOCATIONS:
            raise ValueError(f"watchlist requires 1-{_MAX_LOCATIONS} locations")
        locations: list[AmbientWatchLocation] = []
        seen_locations: set[str] = set()
        for raw in raw_locations:
            if not isinstance(raw, dict):
                raise ValueError("watchlist location must be an object")
            extra = set(raw) - {"id", "label"}
            if extra:
                raise ValueError(f"unknown watchlist location field(s): {', '.join(sorted(extra))}")
            if not {"id", "label"}.issubset(raw):
                raise ValueError("watchlist location requires id and label")
            location_id = bounded_text(raw["id"], "watchlist location id", 64)
            if not _valid_id(location_id) or location_id in seen_locations:
                raise ValueError(f"invalid or duplicate watchlist location id: {location_id}")
            if location_id == UNKNOWN_LOCATION:
                raise ValueError(
                    f"watchlist location id cannot shadow the reserved id {UNKNOWN_LOCATION!r}"
                )
            locations.append(
                AmbientWatchLocation(
                    id=location_id,
                    label=bounded_text(raw["label"], "watchlist location label", 160),
                )
            )
            seen_locations.add(location_id)

        return cls(
            id=watchlist_id,
            title=bounded_text(payload["title"], "watchlist title", 160),
            version=bounded_text(payload["version"], "watchlist version", 64),
            provenance=bounded_text(
                str(payload.get("provenance") or provenance), "watchlist provenance", 200
            ),
            checksum=bounded_text(checksum or "sha256:unverified", "watchlist checksum", 128),
            risk=risk,
            activities=tuple(activities),
            locations=tuple(locations),
        )

    @property
    def activity_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.activities)

    @property
    def location_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.locations)

    def activity_label(self, activity_id: str) -> str:
        for item in self.activities:
            if item.id == activity_id:
                return item.label
        return activity_id

    def location_label(self, location_id: str) -> str:
        for item in self.locations:
            if item.id == location_id:
                return item.label
        return location_id

    def activity_payload(self) -> list[dict[str, Any]]:
        return [
            {"id": item.id, "label": item.label, "visibleEvidence": item.visible_evidence}
            for item in self.activities
        ]

    def location_payload(self) -> list[dict[str, Any]]:
        return [{"id": item.id, "label": item.label} for item in self.locations]

    def public_summary(self) -> dict[str, Any]:
        """What the console may disclose about an active watchlist."""
        return {
            "id": self.id,
            "title": self.title,
            "version": self.version,
            "provenance": self.provenance,
            "checksum": self.checksum,
            "risk": self.risk,
            "activities": [{"id": item.id, "label": item.label} for item in self.activities],
            "locations": self.location_payload(),
        }


class AmbientWatchSession:
    """Per-case ambient watch state; the structural analogue of TaskSession.

    Deliberately has no ``confirm_step``, no checkpoints, no ``completed`` and
    no progress: there is nothing to complete in ambient mode.
    """

    def __init__(
        self,
        watchlist: AmbientWatchList,
        *,
        consent_id: str,
        event_sink: Callable[..., Any] | None = None,
    ) -> None:
        self.watchlist = watchlist
        self.consent_id = bounded_text(consent_id, "ambient consent id", 200)
        self._event_sink = event_sink
        self.watch_epoch = 0
        self.perception_context_id = f"ambient-context-{uuid4().hex}"
        self.observation: dict[str, Any] | None = None
        self.audit: list[dict[str, Any]] = []
        self.n_accepted = 0
        self.n_rejected = 0
        self._submitted_frame_ids: list[str] = []
        self._submitted_frame_set: set[str] = set()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _emit(self, name: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._event_sink is None:
            return {}
        result = self._event_sink(name, payload)
        return result if isinstance(result, dict) else {}

    def reset(self) -> dict[str, Any]:
        """Rotate the context id and bump the epoch, invalidating in-flight frames."""
        self.watch_epoch += 1
        self.perception_context_id = f"ambient-context-{uuid4().hex}"
        self.observation = None
        self._submitted_frame_ids.clear()
        self._submitted_frame_set.clear()
        self._emit("ambient.watch.reset", {"watchEpoch": self.watch_epoch})
        return self.snapshot()

    def ambient_binding(self) -> dict[str, Any]:
        return {
            "contextId": self.perception_context_id,
            "watchlistId": self.watchlist.id,
            "watchlistVersion": self.watchlist.version,
            "watchlistProvenance": self.watchlist.provenance,
            "watchlistChecksum": self.watchlist.checksum,
            "watchEpoch": self.watch_epoch,
            "consentId": self.consent_id,
            "risk": self.watchlist.risk,
        }

    def frame_context(
        self,
        *,
        user_id: str,
        session_id: str,
        frame_id: str,
        captured_at: datetime,
    ) -> AmbientFrameContext:
        return AmbientFrameContext(
            context_id=self.perception_context_id,
            frame_id=frame_id,
            user_id=user_id,
            session_id=session_id,
            watchlist_id=self.watchlist.id,
            watchlist_version=self.watchlist.version,
            watchlist_provenance=self.watchlist.provenance,
            watchlist_checksum=self.watchlist.checksum,
            watch_epoch=self.watch_epoch,
            watch_activities=self.watchlist.activity_payload(),
            watch_locations=self.watchlist.location_payload(),
            risk=self.watchlist.risk,
            consent_id=self.consent_id,
            frame_captured_at=captured_at,
        )

    def should_submit(self, frame_id: str) -> bool:
        """Whether this frame is new. Guards against a stalled camera.

        The realtime bridge keeps the most recent frame and does not clear it,
        so at the slow ambient cadence the same frame would otherwise be
        resubmitted on every tick -- inflating observation counts and burning
        provider calls. Without this, a frozen camera would manufacture
        evidence that a behaviour was observed many times.
        """
        value = str(frame_id or "").strip()
        if not value or value in self._submitted_frame_set:
            return False
        self._submitted_frame_ids.append(value)
        self._submitted_frame_set.add(value)
        if len(self._submitted_frame_ids) > 256:
            evicted = self._submitted_frame_ids.pop(0)
            self._submitted_frame_set.discard(evicted)
        return True

    def ingest(self, evidence: dict[str, Any]) -> dict[str, Any]:
        value = {
            "time": self._now(),
            "status": "accepted",
            "activityId": evidence.get("activityId"),
            "observed": bool(evidence.get("observed", False)),
            "locationId": evidence.get("locationId"),
            "frameId": evidence.get("frameId"),
            "contextId": evidence.get("contextId"),
            "summary": evidence.get("summary"),
        }
        self.observation = value
        self.n_accepted += 1
        self.audit.append(value)
        self.audit = self.audit[-12:]
        self._emit("ambient.observation.received", value)
        return self.snapshot()

    def record_rejection(
        self,
        reason: str,
        *,
        frame_id: str | None = None,
        context_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        value = {
            "time": self._now(),
            "status": "rejected",
            "reason": str(reason),
            "frameId": frame_id,
            "contextId": context_id,
            "detail": dict(detail or {}),
        }
        self.n_rejected += 1
        self.audit.append(value)
        self.audit = self.audit[-12:]
        self._emit("ambient.observation.rejected", value)
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "schemaVersion": AMBIENT_SCHEMA_VERSION,
            "watchlist": self.watchlist.public_summary(),
            "binding": self.ambient_binding(),
            "observation": self.observation,
            "audit": list(reversed(self.audit)),
            "counters": {"accepted": self.n_accepted, "rejected": self.n_rejected},
        }
