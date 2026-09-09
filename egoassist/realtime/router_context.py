"""Project journal evidence into the fields the text-only turn router can use."""
from __future__ import annotations


def project_scene(scene):
    if not scene:
        return scene
    frame = scene.get("frame")
    frames = scene.get("synchronizedFrames") or []
    return {
        "turnId": scene.get("turnId"),
        "speaker": scene.get("speaker"),
        "segment": scene.get("segment"),
        "audioAvailable": bool(scene.get("audio")),
        "frame": {k: frame[k] for k in ("frameId", "receivedAt") if k in frame} if frame else None,
        "synchronizedFrames": {
            "count": len(frames),
            "syncBasis": sorted({f.get("syncBasis", "unknown") for f in frames}),
            "syncAccuracy": sorted({f.get("syncAccuracy", "unmeasured") for f in frames}),
        },
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
