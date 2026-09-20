# EgoLife Historical Visual Retrieval Repair

## Outcome

The historical evidence path now works in the real browser and provider chain.
The final automated suite passed: **657 tests, 3 existing aiohttp deprecation
warnings, 112.19 seconds**. This does **not** establish benchmark correctness:
neither of the two exposed questions has a verified correct, grounded answer.

Open [the recorded verification](../outputs/egolife-history-fix-20260916/index.html).
The updated local application runs at http://127.0.0.1:8778.

## Causes And Changes

- Historical observations were persisted, but long answer-format instructions
  and alternatives diluted retrieval scores. The router now emits the literal
  question span. Ranking uses this span; the full utterance still controls
  temporal, entity and ownership restrictions and the final answer.
- Paused-video questions could have no fresh current frame and only the latest
  observation. A bounded historical visual pass now reads accepted observations
  from the owning case journal, combines caption relevance with chronological
  coverage, and reinspects up to 24 source images using qwen3.5-plus.
- Capture and commit time must precede the question. Ordering compares instants
  across timezone offsets. Source media is resolved inside the case directory
  and its hash is verified. Deleted media fails closed.
- Historical inspection runs after semantic controls and the memory permission
  check, not during speculative scene preparation. It has one request slot,
  a 35-second overall deadline and cancellation logging. Current-frame handling
  remains separate. This path covers explicit questions without a fresh frame;
  it is not full-day retrieval or a replacement for synchronized audio/video.
- Model evidence requires valid source references and a valid response schema.
  The prompt distinguishes similar objects, viewpoint changes and replacement
  claims. References prove provenance, not that the model interpreted pixels
  correctly; visual reasoning remains fallible.
- Browser input now preserves the original video aspect ratio. Replay can
  reconstruct the previous stretched frames from the original video and records
  the source hash. Server capture times are excluded from image event labels.
- Choice scoring rejects multiple or withdrawn declarations. Old unsuccessful
  runs remain intact; a separate rescoring artifact records contradictions.
  A matching choice with unsupported visual reasoning is not a verified success.
- Diagnostic failures are persisted and HTTP resources are closed on errors.

## Actual Browser Verification

Source: `outputs/egolife-history-browser-20260916-r2`.
One minute of original-speed video produced 119 distinct input frames and 12
accepted historical observations. The chain audit requires the exact official
question stem, a real provider request ID, source hashes from the transmitted
video, and the same history result in the answer job context.

The original prediction was frozen before ground truth was revealed. Question
102 answered **D**, while the official answer is **B, 小马座椅**. The response
claimed insufficient evidence while choosing "没有代替". This remains a failure.
The recording includes input, model waiting, the actual answer audio, and the
ground-truth reveal. Synthetic question narration is identified as such.
Original source audio was not supplied.

Visual QA found a inherited display-label error: the recorded overlay says
11:11-11:12, while the original video watermark covers approximately
11:10:42-11:11:42. The gallery explicitly corrects it without altering the
recording. Future recordings label source-file seconds rather than assuming
the input file's event-time origin. Neither label is used as model evidence.

The first browser replay also completed the history call, but its router kept
the full instruction block as the retrieval query. That failed projection was
preserved under `outputs/egolife-history-browser-20260916-r1`, and prompted the
explicit router contract used in r2.

## Component Replays And Remaining Limits

`outputs/visual-history-real-20260916-final` contains actual production-component
calls for questions 81 and 102. These are not a new browser recording. Historical
images were reconstructed without stretching. Question 102 additionally used
the whole 30-second source file `DAY1_A1_JAKE_11163000.mp4`, sampled every three
seconds. This is a target-assisted diagnostic using a disclosed missing segment,
not an independent benchmark or continuous full-history evaluation.

- Question 81 abstained. The silent, obscured-face footage has no independently
  supplied mapping between the visitors and names in the answer options.
- Question 102 still selected D. The model focused on room lighting supports
  rather than reliably connecting the small foreground tripod and later object.
  Finite sampled coverage also omits brief details. Adding the missing segment
  and using a stronger model did not demonstrate a grounded correct answer.

All preceding `visual-history-real-20260916-*` trials are preserved. Some early
responses selected B while describing unsupported objects or later retracting
the answer. The gallery's `replay-rescoring.json` records strict choice matching
separately and does not mark their visual grounding verified. Ground-truth answer
and explanation fields are excluded from generation input.

Further benchmark improvement needs measured object/event retrieval and detail
reinspection, source coverage, and independent identity/audio evidence where
required. Passing code tests alone cannot satisfy that remaining capability goal.

## Reproduction

```powershell
.\.venv\Scripts\python.exe -m pytest -q -o addopts= -p no:cacheprovider
.\.venv\Scripts\python.exe scripts/run_egolife_benchmark_demo.py --output outputs/history-new-run --base-url http://127.0.0.1:8778 --data-root .egoassist-history-demo-r3 --username history-new-run --cases career --media outputs/egolife-full-local-media/egolife-full-local.mp4
.\.venv\Scripts\python.exe scripts/build_history_fix_demo.py --source outputs/history-new-run --output outputs/history-new-video
.\.venv\Scripts\python.exe scripts/verify_story_videos.py --output outputs/history-new-video
```

Use a new output directory for each run. Required video sources, model access,
Playwright Edge, PyAV and ffmpeg must be available. Generated outputs and user
runtime state are local artifacts, not files to commit.
