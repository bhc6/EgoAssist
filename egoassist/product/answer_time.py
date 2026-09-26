"""Local civil times for model-facing evidence; durable sources stay untouched."""
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


TIME_KEYS = frozenset({
    'timestamp', 'timestampWall', 'sourceAt', 'receivedAt', 'recordedAt', 'capturedAt',
    'happenedAt', 'effectiveAt', 'effectiveEnd', 'startAt', 'endAt', 'startedAt',
    'finishedAt', 'updatedAt', 'createdAt', 'statementAt', 'answeringAt',
    'frame_captured_at', 'received_at', 'periodStart', 'periodEnd',
})


def display_zone(clock):
    if not clock:
        return None
    try:
        return ZoneInfo(clock['timezone'])
    except (KeyError, ValueError, TypeError, ZoneInfoNotFoundError):
        offset = clock.get('utcOffsetMinutes')
        if type(offset) in (int, float) and -840 <= offset <= 840:
            return timezone(timedelta(minutes=offset))
        return None


def localize_evidence(value, clock):
    """Convert only explicit zoned timestamps, including JSON-encoded sources.

    Plain prose, dates without offsets, relative dates and original timezone
    metadata retain their source meaning. No guessed +08 offsets or regex edits.
    """
    zone = display_zone(clock)
    def project(item, key='', depth=0):
        if depth > 32:
            return item
        if isinstance(item, dict):
            return {k: project(v, k, depth+1) for k, v in item.items()}
        if isinstance(item, list):
            return [project(v, key, depth+1) for v in item]
        if isinstance(item, str):
            if key in TIME_KEYS and zone:
                try:
                    stamp = datetime.fromisoformat(item.replace('Z', '+00:00'))
                    if stamp.tzinfo is not None:
                        return stamp.astimezone(zone).isoformat()
                except ValueError:
                    pass
            # SourceIndex keeps structured observations in its text field.
            if key == 'text' and item.lstrip().startswith(('{', '[')):
                try:
                    nested = json.loads(item)
                    return json.dumps(project(nested, depth=depth+1), ensure_ascii=False)
                except (ValueError, RecursionError):
                    pass
        return item
    return project(value)


def time_instructions(clock):
    if display_zone(clock) is None:
        return ''
    return ('\nAnswer clock: '+json.dumps(clock, ensure_ascii=False)+
            '\nState clock times in the answer timezone above and name that timezone. '
            'Evidence timestamp offsets have already been converted by code; do not convert them again '
            'or copy a UTC clock hour as local time. Original source timezone metadata still describes '
            'the source, not the answer display. A capture/receipt timestamp dates the sample, not an '
            'unknown event depicted in imported video. Naive timestamps remain unknown-zone. '
            'Missing later samples do not prove no further events; object disappearance does not prove action completion.\n')
