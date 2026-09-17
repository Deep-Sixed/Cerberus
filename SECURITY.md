# Security

Do not put credentials, session databases, provider responses or private
configuration in public issues. Report a vulnerability through this repository's
GitHub private vulnerability reporting facility when enabled. If it is unavailable,
open a details-free issue requesting a private contact channel before sharing
sensitive details. No response-time or supported-version guarantee is established
for the 0.3.0 release.

Expose only the Cerberus API, behind an appropriate TLS endpoint. Configure
caller identities and admin authorization explicitly. Mount credentials from a
secret manager or supply environment references; example values are placeholders.
The fusion backend credential (an OpenRouter API key for the initial backend)
is a provider secret: clients receive only their own `cb-` caller keys and never
the provider credential. A caller with that key could reach the provider
directly, bypassing Cerberus policy, so scope and rotate it as you would any
provider key. An unpublished container port alone does not protect against other
containers on the same network or a privileged host operator.

Admin sessions and pending OIDC logins contain identity and authentication
material. Restrict state-volume access and protect backups. See
`docs/persistence.md` for supported deployment limits and recovery behavior.
