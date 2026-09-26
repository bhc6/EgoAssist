"""Reuse a grounded visual answer only for a standalone visual question."""
from __future__ import annotations
import os


def validate_direct_answer(parsed, labels):
    candidate = parsed.get('directAnswer')
    if not isinstance(candidate, dict) or candidate.get('scope') != 'visible-only':
        return None
    text, refs = candidate.get('text'), candidate.get('sourceIds')
    # A greeting can be returned with an arbitrary frame citation even when the
    # model correctly emitted no visual evidence. Never deliver such a candidate.
    evidence_refs = {ref for fact in parsed.get('evidence', [])
                     for ref in fact.get('sourceIds', [])}
    if (not isinstance(text, str) or not text.strip() or len(text) > 800
            or not isinstance(refs, list) or not refs
            or any(not isinstance(ref, str) or ref not in labels or ref not in evidence_refs for ref in refs)):
        return None
    return {'text': text.strip(), 'sourceIds': [labels[ref] for ref in refs]}


def prepared_visual_answer(scene, semantic, *, task=False, compound=False):
    if os.getenv('EGOASSIST_DIRECT_VISUAL_ANSWERS', '1') == '0':
        return None
    if task or compound or semantic.get('status') != 'classified':
        return None
    if any(semantic.get(key) for key in (
        'memoryQuery', 'memoryQuerySpec', 'memoryText', 'memoryAtoms', 'memoryDraft',
        'memoryEvents', 'preferenceIntent', 'speechControl', 'taskControl',
        'skillControl', 'selectedSkill', 'selectedTask',
    )):
        return None
    intents = semantic.get('intents') or []
    if len(intents) > 1 or any(i.get('kind') != 'foreground-dialogue' for i in intents):
        return None
    interaction = semantic.get('interaction') or {}
    if interaction.get('action') in {'listen', 'pause', 'cancel', 'uncertain', 'keep', 'wait'}:
        return None
    scene = scene or {}
    visual = scene.get('visual') or {}
    answer = visual.get('directAnswer')
    if scene.get('historicalVisual') or visual.get('status') != 'observed' or not answer:
        return None
    return {**answer, 'model': visual['model'], 'providerRequestId': visual.get('requestId'),
            'deliveryPath': 'direct-visual', 'frameEvidence': visual.get('frame'),
            'frameSequenceEvidence': visual.get('frames', [])}
