"""Bounded, question-time audiovisual context owned by the interaction lane."""
from __future__ import annotations

import math
import base64
import io
import json
import os
from copy import deepcopy

from PIL import Image, ImageDraw


LOOKBACK_SECONDS = 12
MAX_FRAMES = 16
BUFFER_SECONDS = 90
BUFFER_BYTES = 32 * 1024 * 1024


def interaction_vision_model(frame_count):
    override = os.getenv('EGOASSIST_INTERACTION_VISION_MODEL', '').strip()
    if override:
        return override
    return 'qwen3.5-plus' if frame_count <= 4 else 'qwen3-vl-plus'


def answer_scene_context(scene):
    """Project evidence for answering; full media and provider audit stay in the journal."""
    result = deepcopy(scene)
    if not result.get('interactionWindow'):
        return result

    def sample(ref):
        if not ref:
            return ref
        return {'sourceId': ref.get('sampleId') or ref.get('frameId'),
                **{key: ref[key] for key in ('receivedAt', 'offsetMs', 'syncBasis', 'syncAccuracy') if key in ref}}

    window = result['interactionWindow']
    window['frames'] = [sample(ref) for ref in window.get('frames', [])]
    window.pop('clockId', None)
    result['synchronizedFrames'] = [sample(ref) for ref in result.get('synchronizedFrames', [])]
    visual = result.get('visual') or {}
    for key in ('frames', 'window', 'presentation', 'rawOutput', 'usage', 'queueMs', 'mediaPreparationMs', 'providerMs', 'latencyMs', 'requestId'):
        visual.pop(key, None)
    if visual.get('evidence'):
        visual.pop('description', None)  # Same facts already have individual source citations.
    if visual.get('frame'):
        visual['frame'] = sample(visual['frame'])
    return result


def visual_sequence_content(scene, frames):
    """Pack dense samples into labeled contact sheets, retaining every original.

    Bound image count as well as pixels. Sheets preserve chronological samples
    with a reversible label-to-source map; the endpoint stays full resolution.
    """
    content, labels, sheets = [], {}, []
    if len(frames) <= 4:
        for index, ref in enumerate(frames):
            source_id = f'F{index+1:02d}'
            labels[source_id] = ref.get('sampleId') or ref['frameId']
            content.extend([{'image': 'data:image/jpeg;base64,' + base64.b64encode(scene.media(ref)).decode()},
                {'text': json.dumps({'sourceId': source_id, 'offsetFromQuestionMs': ref.get('offsetMs')})}])
        return content, labels, sheets
    for offset in range(0, len(frames), 8):
        group = frames[offset:offset+8]
        sheet = Image.new('RGB', (1280, 416), 'white')
        draw = ImageDraw.Draw(sheet)
        for index, ref in enumerate(group):
            label = f'F{offset+index+1:02d}'
            labels[label] = ref.get('sampleId') or ref['frameId']
            x, y = index % 4 * 320, index // 4 * 208
            with Image.open(io.BytesIO(scene.media(ref))) as image:
                image.thumbnail((320, 180))
                sheet.paste(image, (x, y))
            draw.text((x+4, y+184), f'{label}  {ref.get("offsetMs", 0) / 1000:.2f}s', fill='black')
        out = io.BytesIO();sheet.save(out, format='JPEG', quality=88)
        data = out.getvalue()
        ref = scene._save(data, '.jpg', 'image/jpeg')
        sheets.append({'media': ref, 'labels': list(labels)[offset:offset+len(group)], 'tileSize': [320, 180]})
        content.append({'image': 'data:image/jpeg;base64,' + base64.b64encode(data).decode()})
    content.append({'text': 'These contact sheets show chronological video samples, left-to-right then top-to-bottom. '
                    'Each tile has its sourceId F01, F02 etc and seconds before question. Cite those labels. '
                    'All tiles depict distinct sampling times; inspect each separately. Small details may be unreadable at this overview resolution.'})
    content.extend([{'text': f'Full-resolution detail of F{len(frames):02d}, the SAME final sample, not a later moment:'},
        {'image': 'data:image/jpeg;base64,' + base64.b64encode(scene.media(frames[-1])).decode()}])
    return content, labels, sheets


def device_clock(metadata, *, audio=False):
    """Client clocks align media only; they never authorize or date a fact."""
    if not isinstance(metadata, dict):
        return None
    clock = metadata.get('clockId')
    keys = ('startMonotonicMs', 'endMonotonicMs') if audio else ('monotonicMs',)
    values = [metadata.get(key) for key in keys]
    if (not isinstance(clock, str) or not 1 <= len(clock) <= 80
            or any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in values)):
        return None
    if audio and values[1] <= values[0]:
        return None
    return (clock, *[v / 1000 for v in values])


def select_frames(frames, *, end, start=None, clock_id=None, received_before):
    """Prefer dense recent samples, retaining some earlier chronological context.

    Never select frames after the question's audio endpoint, even when ASR is
    delayed. Identical pixels at different times remain distinct samples.
    """
    lower = end - LOOKBACK_SECONDS
    eligible = []
    for frame in frames:
        if frame['monotonic'] > received_before:
            continue
        if clock_id:
            timing = device_clock(frame.get('device'))
            if not timing or timing[0] != clock_id:
                continue
            at = timing[1]
        else:
            at = frame['monotonic']
        if lower <= at <= end:
            eligible.append((at, frame))
    eligible.sort(key=lambda row: row[0])

    def spread(items, budget):
        if len(items) <= budget:
            return items
        return [items[round(i * (len(items) - 1) / (budget - 1))] for i in range(budget)]

    recent = [r for r in eligible if r[0] >= end - 3]
    earlier = [r for r in eligible if r[0] < end - 3]
    # Reserve four earlier points only when they exist. Last-three-second
    # transitions receive most of the budget instead of being averaged away.
    old_budget = min(4, len(earlier))
    selected_recent = spread(recent, MAX_FRAMES - old_budget)
    selected = spread(earlier, MAX_FRAMES - len(selected_recent)) + selected_recent
    selected.sort(key=lambda row: row[0])
    gaps = [{'fromOffsetMs': round((a[0] - end) * 1000),
             'toOffsetMs': round((b[0] - end) * 1000),
             'durationMs': round((b[0] - a[0]) * 1000)}
            for a, b in zip(eligible, eligible[1:]) if b[0] - a[0] > 1]
    return selected, {
        'scope': 'interaction-short-term', 'windowMs': LOOKBACK_SECONDS * 1000,
        'anchor': 'speech-end' if start is not None else 'question-received',
        'syncBasis': 'client-monotonic-estimate' if clock_id else 'server-receipt-estimate',
        'syncAccuracy': 'unmeasured', 'clockId': clock_id,
        'speechStartOffsetMs': round((start - end) * 1000) if start is not None else None,
        'availableFrames': len(eligible), 'selectedFrames': len(selected),
        'sampled': len(selected) < len(eligible), 'gaps': gaps,
        'latestFrameAgeMs': round((end - eligible[-1][0]) * 1000) if eligible else None,
        'coverageMs': round((eligible[-1][0] - eligible[0][0]) * 1000) if eligible else 0,
        'boundary': 'No frames after this question endpoint; unsampled actions are unknown.',
    }
