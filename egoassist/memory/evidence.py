"""Optional, source-grounded indexes over conversation evidence, without an ontology."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping

from .temporal import ClockContext, resolve_time_semantics


def normalize_context(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict) or set(value) - {"entities", "scene", "times"}:
        raise ValueError("invalid evidence context")
    entities = value.get("entities", [])
    times = value.get("times", [])
    scene = value.get("scene")
    if not isinstance(entities, list) or len(entities) > 12:
        raise ValueError("evidence entities must contain at most 12 labels")
    if any(not isinstance(item, str) or not item.strip() or len(item) > 160 for item in entities):
        raise ValueError("invalid evidence entity label")
    if scene is not None and (not isinstance(scene, str) or len(scene) > 240):
        raise ValueError("invalid evidence scene")
    if not isinstance(times, list) or len(times) > 4:
        raise ValueError("evidence times must contain at most four references")
    normalized_times = []
    for index, item in enumerate(times):
        if not isinstance(item, dict) or set(item) - {"relation", "expression", "anchor"}:
            raise ValueError("invalid time reference")
        if any(not isinstance(item.get(key), str) or not item[key].strip() or len(item[key]) > 160
               for key in ("relation", "expression")):
            raise ValueError("time reference requires relation and expression")
        anchor = item.get("anchor", "turn")
        if anchor not in {"turn", "ambiguous", *(str(i) for i in range(index))}:
            raise ValueError("time anchor must be turn, ambiguous or a preceding reference index")
        normalized_times.append({"relation": item["relation"].strip(),
                                 "expression": item["expression"].strip(), "anchor": anchor})
    return {"entities": list(dict.fromkeys(item.strip() for item in entities)),
            "scene": scene.strip() if scene else None, "times": normalized_times}


def ground_context(value: Any, source_text: str, clock: ClockContext) -> dict[str, Any]:
    context = normalize_context(value)
    source = source_text.casefold()
    rejected = []
    entities = []
    for entity in context.get("entities", []):
        if entity.casefold() in source:
            entities.append(entity)
        else:
            rejected.append({"field": "entity", "value": entity, "reason": "not-in-source"})
    scene = context.get("scene")
    if scene and scene.casefold() not in source:
        rejected.append({"field": "scene", "value": scene, "reason": "not-in-source"})
        scene = None
    references = []
    for item in context.get("times", []):
        resolved = {**item, "start": None, "end": None, "status": "unresolved"}
        if item["expression"].casefold() not in source or item["relation"].casefold() not in source:
            resolved["reason"] = "not-in-source"
        elif item["anchor"] == "ambiguous":
            resolved["reason"] = "ambiguous-anchor"
        else:
            anchor_clock = clock
            if item["anchor"] != "turn":
                previous = references[int(item["anchor"])]
                anchor_clock = (ClockContext.current(clock.timezone_name, clock.utc_offset_minutes,
                                now_utc=datetime.fromisoformat(previous["start"]))
                                if previous["start"] else None)
            if anchor_clock is not None:
                semantics = resolve_time_semantics(item["expression"], anchor_clock, kind=None)
                resolved.update(start=semantics["effectiveAt"], end=semantics["effectiveEnd"])
                if resolved["start"]:
                    resolved["status"] = "resolved"
                else:
                    resolved["reason"] = "unsupported-expression"
            else:
                resolved["reason"] = "unresolved-anchor"
        references.append(resolved)
    return {"entities": entities, "scene": scene, "times": references,
            "rejected": rejected, "clock": clock.prompt_payload(),
            "status": "source-grounded-labels", "calibratedProbability": None}


def time_matches(query: Mapping[str, Any], candidate: Mapping[str, Any]) -> bool:
    if not query.get("start") or not candidate.get("start"):
        return False
    start = datetime.fromisoformat(query["start"])
    upper = datetime.fromisoformat(query["end"]) if query.get("end") else start + timedelta(minutes=1)
    other = datetime.fromisoformat(candidate["start"])
    end = datetime.fromisoformat(candidate["end"]) if candidate.get("end") else None
    return other < upper and end > start if end else start <= other < upper


def evidence_network(cards: list[dict[str, Any]]) -> dict[str, Any]:
    """Project co-mentioned labels and event records; never infer interpersonal ties."""
    nodes: dict[str, dict[str, Any]] = {}
    events = []
    for card in cards:
        evidence = card.get("metadata", {}).get("evidenceContext", {})
        labels = evidence.get("entities", [])
        for label in labels:
            node = nodes.setdefault(label, {"label": label, "cardIds": []})
            node["cardIds"].append(card["id"])
        if labels or evidence.get("scene"):
            events.append({"cardId": card["id"], "entities": labels, "scene": evidence.get("scene"),
                           "relation": card.get("predicate"), "text": card["text"],
                           "times": evidence.get("times", []), "sourceIds": card.get("sourceIds", [])})
    return {"nodes": list(nodes.values()), "events": events, "calibratedProbability": None,
            "claimBoundary": "Source-label co-occurrence; identical names may be different people. No identity merge or social tie is inferred."}
