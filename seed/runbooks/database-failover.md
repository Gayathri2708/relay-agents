# Runbook: orders database failover

The orders database is PostgreSQL 16 with one primary (orders-db-1) and two streaming replicas (orders-db-2, orders-db-3) managed by Patroni. payments-api, checkout and order history all depend on it. Owner: team data-platform, PagerDuty service DBA.

## [DB-1] Health checks
Check, in order: is the primary accepting writes (pg_up and patroni_primary), replication lag (pg_replication_lag_seconds), connection count against max_connections 500, and disk usage on the primary (alert at 85%). Long-running queries over 60 seconds often cause lock pileups; list them in pg_stat_activity.

## [DB-2] When to fail over
Fail over only if the primary is unreachable for more than 2 minutes, or its disk is full and cannot be extended quickly. Do not fail over for high CPU or slow queries; fix the query instead. A failover drops in-flight transactions and can lose up to the replication lag in data, so check lag first: the target replica must be less than 10 seconds behind.

## [DB-3] Failover procedure
Failover is a high-risk action. It requires approval from the data-platform on-call and the incident commander. Steps: confirm the target replica lag is under 10 seconds; run patronictl failover --candidate orders-db-2; verify the new primary accepts writes; confirm payments-api and checkout reconnect (their pools retry for 60 seconds); post the timeline in #incidents.

## [DB-4] Replica lag
If replica lag exceeds 60 seconds, read traffic from order history may serve stale data. Check for long-running queries on the replica and for heavy write bursts on the primary. Lag alone is P2 at most unless a failover is needed.

## [DB-5] Connection exhaustion
If connections are near max_connections, find which service holds them (pg_stat_activity grouped by application_name). Do not raise max_connections during an incident; it requires a restart. Instead ask the owning team to scale down or fix their pool, and terminate idle-in-transaction sessions older than 5 minutes, which needs approval.
