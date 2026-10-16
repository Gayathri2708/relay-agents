# Runbook: auth-service

auth-service handles login, session tokens (JWT, signed with a key rotated every 30 days) and password resets. Owner: team identity, PagerDuty service IDN.

## [AUTH-1] Triage checklist
Check login success rate (auth_login_success_ratio), token validation errors, and the last auth-service deploy. A sudden spike of "invalid signature" errors across all services usually means a signing key rotation problem, not a code bug.

## [AUTH-2] Signing key rotation failure
If token validation fails with "invalid signature" or "unknown kid" right after a scheduled key rotation, re-publish the previous signing key to the JWKS endpoint so both keys are valid. This is a risky change to security configuration and needs approval from the identity lead and the incident commander. Never disable token validation as a workaround.

## [AUTH-3] Login failures after a deploy
If login success dropped within 30 minutes of an auth-service deploy, roll back following [DEP-2]. Rollback needs approval for P1 and P2.

## [AUTH-4] Credential stuffing or suspicious traffic
If failed logins spike from a small set of IPs or ASNs, this may be an attack. Treat as P1 per [SEV-5] if any account takeover is suspected, and page security on-call (SEC). Enabling the rate-limit flag auth.login.strict_rate_limit is a risky action and needs approval. Never paste raw logs with tokens or customer emails into chat or tickets.

## [AUTH-5] Who to page
Page team identity (IDN). For suspected attacks also page security on-call (SEC).
