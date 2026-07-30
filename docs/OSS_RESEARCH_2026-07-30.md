# Open Source Foundation Research Snapshot

Snapshot date: 2026-07-30 (Asia/Shanghai)
Decision context: [PROJECT_CHARTER_2026.md](PROJECT_CHARTER_2026.md)

## Method

The snapshot was collected from official GitHub repository metadata, license
endpoints, latest release endpoints, and official documentation. Star counts are
volatile and are used only as one adoption signal. Selection also considers
release recency, license clarity, Python/browser fit, operational ownership, and
overlap with other selected components.

All selected repositories were unarchived and had recent activity when checked.
Official documentation links below returned HTTP 200 on the snapshot date. The
MCP documentation redirected to its dated `2026-07-28` documentation snapshot.

## Candidate Snapshot

| Project | Stars | Latest release | Release date | License | Decision |
|---|---:|---|---|---|---|
| [LiveKit Agents](https://github.com/livekit/agents) | 11,554 | `livekit-agents@1.6.7` | 2026-07-25 | Apache-2.0 | Select |
| [LiveKit server](https://github.com/livekit/livekit) | 20,021 | `v1.13.4` | 2026-07-18 | Apache-2.0 | Select |
| [LiveKit JS client](https://github.com/livekit/client-sdk-js) | 645 | `v2.21.0` | 2026-07-23 | Apache-2.0 | Select |
| [Pipecat](https://github.com/pipecat-ai/pipecat) | 13,790 | `v1.6.0` | 2026-07-21 | BSD-2-Clause | Defer: overlaps LiveKit interaction runtime |
| [Temporal server](https://github.com/temporalio/temporal) | 21,934 | `v1.31.2` | 2026-07-08 | MIT | Select |
| [Temporal Python SDK](https://github.com/temporalio/sdk-python) | 1,146 | `1.31.0` | 2026-07-29 | MIT | Select |
| [Restate](https://github.com/restatedev/restate) | 4,233 | `v1.7.2` | 2026-07-06 | Other / NOASSERTION | Defer pending license and migration benefit |
| [LangGraph](https://github.com/langchain-ai/langgraph) | 38,426 | `1.2.10` | 2026-07-28 | MIT | Defer: avoid a second lifecycle engine |
| [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) | 23,775 | `v2.0.0` | 2026-07-28 | MIT | Select for external connectors |
| [APScheduler](https://github.com/agronholm/apscheduler) | 7,582 | `3.11.3` | 2026-06-28 | MIT | Local experiments only |
| [ntfy](https://github.com/binwiederhier/ntfy) | 32,365 | `v2.26.3` | 2026-07-20 | Apache-2.0 | Select as first push adapter |
| [Presidio](https://github.com/microsoft/presidio) | 10,252 | `2.2.364` | 2026-07-22 | MIT | Select as PII defense in depth |
| [Chatwoot](https://github.com/chatwoot/chatwoot) | 34,870 | `v4.16.2` | 2026-07-27 | Other / NOASSERTION | Optional connector only; do not embed |
| [OpenTelemetry Python](https://github.com/open-telemetry/opentelemetry-python) | 2,563 | `v1.44.0` | 2026-07-16 | Apache-2.0 | Select |
| [SQLAlchemy](https://github.com/sqlalchemy/sqlalchemy) | 12,034 | `2.0.51` | 2026-06-15 | MIT | Select |
| [Alembic](https://github.com/sqlalchemy/alembic) | 4,285 | `1.18.5` | 2026-06-25 | MIT | Select |
| [Pydantic](https://github.com/pydantic/pydantic) | 28,430 | `v2.13.4` | 2026-05-06 | MIT | Select |
| [PDF.js](https://github.com/mozilla/pdf.js) | 53,641 | `v6.2.108` | 2026-07-28 | Apache-2.0 | Select for PDF artifacts |
| [PhotoSwipe](https://github.com/dimsemenov/PhotoSwipe) | 25,224 | No GitHub latest release | last push 2025-12-04 | MIT | Candidate image viewer |

## Selection Rationale

### LiveKit over a custom WebSocket media stack

LiveKit provides a self-hostable realtime server, browser SDK, and agent runtime
under Apache-2.0. It owns reconnect, room membership, media transport, device
clients, and data channels. EgoAssist keeps provider-specific model adapters and
interaction policy behind an `InteractionTransport` port.

Pipecat remains a credible alternative. Selecting both now would duplicate
pipeline/session ownership and make interruption debugging harder.

### Temporal over an in-process scheduler

Temporal owns durable timers, workflow replay, retries, cancellation, signals,
and activity idempotency boundaries. The Python SDK is current and MIT licensed.
This directly addresses accepted jobs that must outlive an aiohttp process or
browser connection.

APScheduler remains useful for unit tests and local prototypes but is not the
product lifecycle owner. Restate is active, but it does not currently justify a
second migration path given the selected Temporal stack and license clarity.

### No LangGraph in the first migration

Intent decomposition and policy are domain logic, not a reason to introduce a
second durable state graph. Temporal workflows can call deterministic EgoAssist
planning code. LangGraph may be reconsidered for bounded, non-authoritative model
planning after the execution architecture is stable.

### MCP only at the connector boundary

The official Python SDK is selected for external interoperability and connector
discovery. Internal memory, policy, and workflow calls remain typed Python domain
ports. This avoids turning every local operation into a remote protocol hop.

### Data and user experience foundations

SQLAlchemy, Alembic, and Pydantic provide versioned product schemas and reversible
migrations. OpenTelemetry provides vendor-neutral correlation across realtime and
workflow planes. ntfy is the first self-hostable push adapter. PDF.js provides a
mature reference viewer. Presidio assists redaction, but Chinese and domain PII
require EgoAssist recognizers and explicit consent policy.

## Official Documentation Checked

- [LiveKit Agents documentation](https://docs.livekit.io/agents/)
- [Temporal Python SDK developer guide](https://docs.temporal.io/develop/python)
- [MCP documentation](https://modelcontextprotocol.io/docs/getting-started/intro)
- [ntfy documentation](https://docs.ntfy.sh/)
- [Presidio documentation](https://microsoft.github.io/presidio/)
- [OpenTelemetry Python documentation](https://opentelemetry.io/docs/languages/python/)
- [Alembic documentation](https://alembic.sqlalchemy.org/)
- [PDF.js documentation](https://mozilla.github.io/pdf.js/)

## Compatibility Decisions Recorded During Implementation

- The latest OpenTelemetry release observed in this snapshot remains `1.44.0`,
  but LiveKit Agents `1.6.7` currently resolves the Python runtime to OTel
  `1.39.1` and protobuf `6.33.6`. EgoAssist pins that tested compatible set in
  `pyproject.toml` and `requirements-lock.txt`; this is an intentional
  compatibility lock, not a claim that 1.39 is latest.
- `livekit-client@2.21.0` is locked by `package-lock.json` and its official UMD
  distribution is vendored for offline console delivery. Runtime CDN loading is
  not used.
- LiveKit is limited to room/media/reconnect ownership. Temporal remains the
  durable workflow owner, while EgoAssist retains intent, memory, safety, and
  output arbitration. Pipecat and LangGraph remain deferred to avoid duplicate
  lifecycle engines.

## Revalidation Policy

- Recheck versions, security advisories, licenses, and maintenance status before
  adding any dependency to `pyproject.toml` or deployment manifests.
- Revalidate this snapshot every 90 days while the migration is active.
- Pin exact production versions through a lock file and record upgrades in an ADR.
- Run a two-week spike for LiveKit and Temporal before deleting the existing paths.
- Do not use star count as a release gate.
