"""Pure validation for memory capture proposals."""
from typing import Any, Mapping
from .timeline import normalize_identity

def memory_draft_candidate(
    text: str,
    draft: Mapping[str, Any] | None,
    pending: Mapping[str, Any] | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate draft identity and progress; intent belongs to the semantic model."""
    if not draft:
        return None, None
    candidate = dict(draft)
    if candidate.get("mode") == "start":
        if pending:
            return None, "draft-start-conflicts-with-pending-draft"
        return candidate, None
    if not pending:
        return None, "draft-continuation-without-pending-draft"
    expected_predicate = normalize_identity(pending.get("predicate"))
    candidate_predicate = normalize_identity(candidate.get("predicate"))
    expected_subject = normalize_identity(pending.get("subject"))
    candidate_subject = normalize_identity(candidate.get("subject"))
    if not candidate_predicate or candidate_predicate != expected_predicate:
        return None, "draft-continuation-predicate-mismatch"
    if expected_subject and candidate_subject != expected_subject:
        return None, "draft-continuation-subject-mismatch"
    before_missing = set(pending.get("missingFields") or ())
    after_missing = set(candidate.get("missingFields") or ())
    subject_progress = not expected_subject and bool(candidate_subject)
    if not after_missing < before_missing and not subject_progress:
        return None, "draft-continuation-made-no-progress"
    return candidate, None
