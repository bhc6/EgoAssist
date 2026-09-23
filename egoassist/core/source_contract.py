"""Versioned source identity shared across lanes; payloads remain in journals."""
from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar


@dataclass(frozen=True)
class SourceReference:
    schema_version: ClassVar[str] = 'source-reference.v1'
    user_id: str
    session_id: str
    event_id: str
    recorded_at: str
    source_at: str | None
    idempotency_key: str | None = None

    def __post_init__(self):
        if not all(isinstance(v, str) and v.strip() for v in (self.user_id, self.session_id, self.event_id)):
            raise ValueError('source ownership is required')
        stamp = datetime.fromisoformat(self.recorded_at.replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('recorded_at requires a timezone')
        if self.source_at is not None:
            datetime.fromisoformat(self.source_at.replace('Z', '+00:00'))

    @classmethod
    def from_entry(cls, user, session, row):
        metadata = row.get('metadata') or (row.get('message') or {}).get('metadata') or {}
        source_at = (row.get('source') or {}).get('source_at') if row.get('type') == 'source.event' else (
            metadata.get('sourceRecordedAt') or (row.get('evidence') or {}).get('capturedAt') or row['timestamp'])
        return cls(user, session, row['id'], row['timestamp'], source_at, row.get('idempotencyKey'))

    @property
    def source_id(self):
        return self.session_id + ':' + self.event_id
