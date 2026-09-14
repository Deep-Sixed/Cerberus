# Security

Do not put credentials, session databases, provider responses or private
configuration in public issues. Report a vulnerability through this repository's
GitHub private vulnerability reporting facility when enabled. If it is unavailable,
open a details-free issue requesting a private contact channel before sharing
sensitive details. No response-time or supported-version guarantee is established
for the 0.01 release candidate.

Expose only the Cerberus API, behind an appropriate TLS endpoint. Configure
caller identities and admin authorization explicitly. Mount credentials from a
secret manager or supply environment references; example values are placeholders.
Use a distinct worker bearer token that clients do not receive. Never publish the
Fusion worker port or expose its management endpoints through a reverse proxy.
An unpublished container port alone does not protect against other containers on
the same network or a privileged host operator.

Admin sessions and pending OIDC logins contain identity and authentication
material. Restrict state-volume access and protect backups. See
`docs/persistence.md` for supported deployment limits and recovery behavior.
