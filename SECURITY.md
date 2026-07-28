# Security Policy

EgoAssist is a research prototype and is not designed for public-network deployment.

Do not include API keys, personal videos, model credentials, or private dataset paths in issues.
Report security-sensitive findings privately to the repository maintainer through GitHub's
private vulnerability reporting feature when enabled.

The local HTTP servers have no authentication, TLS, rate limiting, or upload hardening. Bind them
to `127.0.0.1` unless you have added an appropriate production gateway.
