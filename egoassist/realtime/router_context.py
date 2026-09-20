"""Project journal evidence into the fields the text-only turn router can use."""
from __future__ import annotations


def retrieval_text(transcript, semantic):
    """Use a literal single question span for ranking, never an invented rewrite.

    The complete utterance still owns permission, temporal constraints and the
    answer. Multiple intents or non-verbatim router paraphrases keep the input.
    """
    intents = (semantic or {}).get('intents') or []
    if len(intents) == 1 and intents[0].get('kind') == 'foreground-dialogue':
        text = str(intents[0].get('text') or '').strip()
        if len(text) >= 4 and text in transcript:
            return text
    return transcript


def project_task(task):
    """Keep the completion criterion alongside the current checkpoint identity."""
    if not task:
        return None
    result = {key: task.get(key) for key in ('task', 'currentCheckpointId', 'paused', 'completed')}
    step = next((step for step in task.get('steps', [])
                 if step.get('id') == task.get('currentCheckpointId')), None)
    result['currentStep'] = ({key: step.get(key) for key in
                              ('id', 'title', 'instruction', 'completion', 'status')} if step else None)
    return result


def project_scene(scene):
    if not scene:
        return scene
    frame = scene.get("frame")
    frames = scene.get("synchronizedFrames") or []
    synchronization = (dict(frames) if isinstance(frames, dict) else {
        "count": len(frames),
        "syncBasis": sorted({f.get("syncBasis", "unknown") for f in frames}),
        "syncAccuracy": sorted({f.get("syncAccuracy", "unmeasured") for f in frames}),
    })
    return {
        "turnId": scene.get("turnId"),
        "observationContext": scene.get("observationContext"),
        "speaker": scene.get("speaker"),
        "segment": scene.get("segment"),
        "audioAvailable": bool(scene.get("audio") or scene.get('audioAvailable')),
        "frame": {k: frame[k] for k in ("frameId", "receivedAt") if k in frame} if frame else None,
        "synchronizedFrames": synchronization,
        "avAssociation": scene.get("avAssociation"),
        "utterances": [{k: u[k] for k in ("text", "status", "speakerId", "name", "wearer", "startMs", "endMs", "cluster") if k in u}
                       for u in scene.get("utterances", [])],
    }


def project_context(context):
    result = dict(context)
    result["scene"] = project_scene(context.get("scene"))
    previous = context.get("previousTurn")
    if previous:
        result["previousTurn"] = {k: previous[k] for k in
            ("turnId", "text", "status", "source", "sequence", "timestamp") if k in previous}
    # Playback is already a separate field. Keep the permission itself intact.
    permission = context.get("outputPermission")
    if permission and permission.get("playback") == context.get("playback"):
        result["outputPermission"] = {k: v for k, v in permission.items() if k != "playback"}
    return result
