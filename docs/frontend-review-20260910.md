# Frontend Logic Review: 2026-09-10

## Changes

- Bind text submissions and HTTP state responses to the originating user, session,
  and session revision. Switching away and back invalidates earlier responses.
- Capture session selection during the DOM event. Keep drafts per session while
  the page stays open, and clear optimistic history when changing sessions.
- Stop waiting for cancelled provider connections when switching cases. Preserve
  text-only operation instead of unexpectedly requesting media permissions.
- Persist outgoing text before showing an optimistic row. Until a server receipt
  arrives, identify that row as pending, rather than claiming server acceptance.
- Fence report requests so late responses cannot replace a newer selection.
  Read report headings from `period` and show source-local timestamps.
- Recover stalled WebSocket state refresh through bounded HTTP polling, with a
  visible status message. Reset refresh bookkeeping on connection changes.
- Clean up local VAD when devices stop, prevent duplicate microphone processors,
  and fence late LiveKit setup against connection and media changes.
- Show realtime errors inline so provider errors do not freeze the event loop.

## Verification

All 424 Python tests passed, with no failures, errors, or skips. Existing aiohttp
deprecation/key warnings and a pytest cache permissions warning remain.
`outputs/frontend-review-tests.xml` contains the test results. This suite ran
before the final frontend-only refinement; browser checks below cover that code.

Final browser runs completed successfully and include manifests and provenance:

- `outputs/runs/20260910T035401Z-frontend-races-final-4148388e`: eight deliberately
  injected UI race/recovery checks. These are not model validation.
- `outputs/runs/20260910T035220Z-frontend-qwen-final-7e0a6532`: real Qwen text,
  visual question, EgoLife recorded audio, browser playback samples, refresh,
  history scrolling, and 1440/390-pixel layouts. No provider mocks.
- `outputs/runs/20260910T035224Z-frontend-reports-final-374fae3b`: real daily,
  weekly, monthly reports, suggestions, skills, reconnect, pending text recovery,
  and desktop/mobile browser checks. No page errors or failed requests.

Final Qwen sample measurements (one turn per category, not percentile estimates):

| Measurement | Text calculation | Visual question |
| --- | ---: | ---: |
| Server input to first provider audio | 1491 ms | 1515 ms |
| Server input to generation complete | 1508 ms | 2224 ms |
| Browser submit through observed completed answer | 5408.4 ms | 2412.8 ms |

The first text turn includes connection startup and state propagation. Browser
completion is not the instant the user first hears sound. Playback audit confirms
scheduled samples advanced in the browser; physical speaker sound was not measured.

## Remaining Work

- First-turn connection startup is still a material delay. Measure connection,
  client submission, server acceptance, and playback separately before choosing a
  preconnection policy; a live realtime connection consumes provider resources.
- Tool confirmation still uses synchronous `window.confirm`. Replace it with a
  nonblocking, call-scoped dialog and test concurrent tool requests and disconnects.
- Unsent drafts survive case switching in the current page only. Accepted requests
  use the durable server ledger; unacknowledged submissions use the existing outbox.
- Actual LiveKit service recovery and physical glasses remain unvalidated in these
  runs. The real media tests use recorded dataset input and WebSocket transport.
