# Real E2E blockers (2026-09-08)

- QWEN_API_KEY is configured (secret not recorded).
- DashScope DNS resolves to public IPs after Mihomo virtual adapter shutdown.
- Python TCP connect to DashScope:443 still fails with Windows `WinError 10013`; the created outbound firewall rule is not enforced (`EnforcementStatus: NotApplicable`).
- Qwen Realtime therefore reaches `provider_unavailable` after the 5 second connect timeout; no token/audio latency sample exists.
- EgoLife DashScope benchmark initialization also requires local `torch`/CLIP encoder, which is not installed in `.venv`; it exits before the first API request.

No real-model metric is reported as successful.
