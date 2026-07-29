# Security Policy

EgoAssist is a research prototype and is not designed for public-network deployment.

Do not include API keys, personal videos, model credentials, or private dataset paths in issues.
Report security-sensitive findings privately to the repository maintainer through GitHub's
private vulnerability reporting feature when enabled.

The local console requires a participant login so experiment cases, journals, and memories do not
mix across research participants. This is an experiment-isolation and audit mechanism, not a
production authentication system. The service has no TLS, rate limiting, account-recovery flow, or
deployment hardening. Bind it to `127.0.0.1` unless you have added an appropriate production gateway.
