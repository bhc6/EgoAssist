"""Erase source payloads before rebuilding any derived state."""

from collections import defaultdict, deque
from datetime import datetime, timezone

from .events import owned_identifiers, references, retention_categories


def erase_expired(journal, cutoffs, *, source_ids=()):
    erased = set(source_ids)
    turns = set()

    def transform(rows):
        dependents = defaultdict(set)
        # Older answer audits serialized their context. Recover dependencies from
        # the structured job, without interpreting arbitrary prompt strings.
        answer_refs = defaultdict(set)
        for row in rows:
            if row.get("type") == "answer.job" and row.get("turnId"):
                answer_refs[row["turnId"]].update(references(row.get("context", {})))
        identifiers = []
        pending = deque()
        for index, row in enumerate(rows):
            refs = references(row)
            if row.get("type", "").startswith("answer."):
                refs.update(answer_refs.get(row.get("turnId"), ()))
            # A case ID is a scope, never a dependency on every event in the case.
            refs.discard(journal.session_id)
            identifiers.append(owned_identifiers(row))
            for ref in refs:
                dependents[ref].add(index)
            if row.get("type") == "retention.deleted":
                erased.update(row.get("erasedIds", []))
                turns.update(row.get("turnIds", []))
                continue
            applicable = [cutoffs[category] for category in retention_categories(row) if category in cutoffs]
            cutoff = max(applicable) if applicable else None
            timestamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if cutoff is not None and timestamp < cutoff:
                pending.append(index)
        for ref in tuple(erased):
            pending.extend(dependents.get(ref, ()))
        removed = set()
        while pending:
            index = pending.popleft()
            if index in removed or rows[index].get("type") == "retention.deleted":
                continue
            removed.add(index)
            row = rows[index]
            if row.get("turnId"):
                turns.add(row["turnId"])
            for ref in identifiers[index]:
                if ref not in erased:
                    erased.add(ref)
                    pending.extend(dependents[ref])
        for index in removed:
            row = rows[index]
            rows[index] = {
                "type": "retention.deleted",
                "id": row["id"],
                "timestamp": row["timestamp"],
                "originalType": row["type"],
                "erasedIds": sorted(identifiers[index]),
                "turnIds": [row["turnId"]] if row.get("turnId") else [],
                "mediaIds": sorted(
                    ref for ref in references(row) if ref.endswith((".jpg", ".wav"))
                ),
                **(
                    {"idempotencyKey": row["idempotencyKey"]}
                    if row.get("idempotencyKey")
                    else {}
                ),
            }
        return rows

    journal.rewrite(transform)
    return {"sourceIds": erased, "turnIds": turns}
