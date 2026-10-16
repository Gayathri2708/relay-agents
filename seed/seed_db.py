"""Seed the relay database with realistic fake operational data for Brightcart.

Four scenarios line up with the sample alerts in seed/alerts/:
  payments-5xx-after-deploy.txt   2026-10-14 14:02Z  bad payments-api deploy (KeyError in wallet path)
  checkout-latency-sale.txt       2026-10-15 19:30Z  checkout saturated by flash-sale traffic, no deploy
  auth-invalid-signature.txt      2026-10-16 02:10Z  JWT signing key rotation broke token validation
  orders-db-replica-lag.txt       2026-10-12 09:15Z  replica lag from a long analytics query

Log lines deliberately contain fake secrets and customer emails so redaction is exercised.
Re-running wipes and reloads the seed tables (runs and decisions are kept).

    python -m seed.seed_db
"""

import json
from datetime import UTC, datetime, timedelta

from relay.config import get_settings
from relay.db import apply_schema, make_pool


def t(s: str) -> datetime:
    return datetime.fromisoformat(s).replace(tzinfo=UTC)


HEALTHY = {
    "payments-api": {
        "error_rate_pct": 0.3, "p99_latency_ms": 420, "rps": 310, "cpu_pct": 38,
        "processor_error_rate_pct": 0.4, "db_pool_in_use_pct": 41, "replicas": 6,
    },
    "checkout": {"error_rate_pct": 0.2, "p99_latency_ms": 900, "rps": 520, "cpu_pct": 46, "replicas": 8},
    "auth-service": {
        "login_success_ratio": 0.985, "token_validation_errors_per_min": 3, "p99_latency_ms": 180, "rps": 900,
    },
    "orders-db": {
        "primary_up": 1, "replication_lag_s": 0.4, "connections": 210, "max_connections": 500,
        "disk_used_pct": 62, "cpu_pct": 35,
    },
}

# (service, start, end, overrides by minute offset from start). Every 5 minutes gets a snapshot.
SCENARIO_WINDOWS = [
    ("2026-10-14T13:30", "2026-10-14T14:15"),
    ("2026-10-15T19:00", "2026-10-15T19:45"),
    ("2026-10-16T01:40", "2026-10-16T02:25"),
    ("2026-10-12T08:45", "2026-10-12T09:30"),
]

ANOMALIES: dict[tuple[str, str], dict] = {
    # payments-api errors spike 15 minutes after the 13:47 deploy; processor and DB pool stay normal.
    ("payments-api", "2026-10-14T14:00"): {"error_rate_pct": 6.2, "p99_latency_ms": 610},
    ("payments-api", "2026-10-14T14:05"): {"error_rate_pct": 14.8, "p99_latency_ms": 640},
    ("payments-api", "2026-10-14T14:10"): {"error_rate_pct": 16.1, "p99_latency_ms": 655},
    ("payments-api", "2026-10-14T14:15"): {"error_rate_pct": 15.7, "p99_latency_ms": 650},
    # checkout saturates under flash-sale traffic; dependencies healthy.
    ("checkout", "2026-10-15T19:20"): {"rps": 1050, "cpu_pct": 78, "p99_latency_ms": 2100},
    ("checkout", "2026-10-15T19:25"): {"rps": 1340, "cpu_pct": 88, "p99_latency_ms": 3400, "error_rate_pct": 0.7},
    ("checkout", "2026-10-15T19:30"): {"rps": 1410, "cpu_pct": 93, "p99_latency_ms": 4200, "error_rate_pct": 1.1},
    ("checkout", "2026-10-15T19:35"): {"rps": 1460, "cpu_pct": 94, "p99_latency_ms": 4650, "error_rate_pct": 1.4},
    ("checkout", "2026-10-15T19:40"): {"rps": 1430, "cpu_pct": 94, "p99_latency_ms": 4500, "error_rate_pct": 1.3},
    ("orders-db", "2026-10-15T19:30"): {"connections": 285, "cpu_pct": 52},
    # auth: key rotation at 02:00, validation errors explode, login success collapses.
    ("auth-service", "2026-10-16T02:05"): {"login_success_ratio": 0.62, "token_validation_errors_per_min": 4100},
    ("auth-service", "2026-10-16T02:10"): {"login_success_ratio": 0.41, "token_validation_errors_per_min": 9800},
    ("auth-service", "2026-10-16T02:15"): {"login_success_ratio": 0.39, "token_validation_errors_per_min": 10400},
    ("auth-service", "2026-10-16T02:20"): {"login_success_ratio": 0.40, "token_validation_errors_per_min": 10100},
    ("checkout", "2026-10-16T02:10"): {"error_rate_pct": 9.5},
    # orders-db replica lag from a reporting query.
    ("orders-db", "2026-10-12T09:05"): {"replication_lag_s": 22},
    ("orders-db", "2026-10-12T09:10"): {"replication_lag_s": 61},
    ("orders-db", "2026-10-12T09:15"): {"replication_lag_s": 95},
    ("orders-db", "2026-10-12T09:20"): {"replication_lag_s": 128},
    ("orders-db", "2026-10-12T09:25"): {"replication_lag_s": 141},
}

DEPLOYS = [
    ("checkout", "v5.12.0", "v5.11.4", "marco.r", "9c1e4a2", "Add gift-card balance to cart summary", False,
     "2026-10-14T11:20"),
    ("payments-api", "v2.41.0", "v2.40.3", "priya.n", "e7b02d9",
     "Refactor idempotency key handling for wallet payments", False, "2026-10-14T13:47"),
    ("auth-service", "v3.8.2", "v3.8.1", "sam.k", "41aa0c3", "Bump password hashing library", False,
     "2026-10-14T16:05"),
    ("checkout", "v5.12.1", "v5.12.0", "marco.r", "b3f9e10", "Fix tax rounding for CA orders", False,
     "2026-10-15T10:02"),
    ("orders-db", "migration-0142", "migration-0141", "lena.w", "77d3c21",
     "Add index on payments(created_at)", True, "2026-10-15T14:30"),
    ("auth-service", "keys-2026-10", "keys-2026-09", "jwks-rotator", "auto",
     "Scheduled JWT signing key rotation (kid k-2026-10)", False, "2026-10-16T02:00"),
    ("payments-api", "v2.39.0", "v2.38.2", "dev.a", "1d0f6b8", "Retry budget for processor calls", False,
     "2026-10-12T07:10"),
]

LOGS = [
    # payments-api: the bad deploy. Contains a fake Stripe key, bearer token and customer emails.
    ("payments-api", "2026-10-14T13:59:12", "ERROR",
     "POST /v1/payments/authorize 500 KeyError: 'idempotency_key' in wallet_authorize "
     "(payments/handlers/wallet.py:212) request_id=8f2c1 customer=jane.doe@example.com"),
    ("payments-api", "2026-10-14T14:01:40", "ERROR",
     "POST /v1/payments/authorize 500 KeyError: 'idempotency_key' in wallet_authorize "
     "(payments/handlers/wallet.py:212) request_id=8f2d7 customer=a.ng@example.org"),
    ("payments-api", "2026-10-14T14:02:05", "WARN",
     "processor call ok after retry; headers Authorization: Bearer dp_FAKEdemoKEY7f3a9c2e1b4d latency=212ms"),
    ("payments-api", "2026-10-14T14:04:30", "ERROR",
     "wallet payment failed for order 100482211 api_key=dp_FAKEdemoKEY7f3a9c2e1b4d "
     "KeyError: 'idempotency_key'"),
    ("payments-api", "2026-10-14T14:06:51", "ERROR",
     "POST /v1/payments/authorize 500 KeyError: 'idempotency_key' in wallet_authorize "
     "(payments/handlers/wallet.py:212) request_id=8f3a0"),
    # checkout: saturation.
    ("checkout", "2026-10-15T19:24:10", "WARN", "recommendations widget render took 1840ms (budget 300ms)"),
    ("checkout", "2026-10-15T19:28:44", "WARN", "worker pool saturated: 64/64 busy, queue depth 412"),
    ("checkout", "2026-10-15T19:31:02", "ERROR",
     "POST /api/checkout/place-order timeout after 5000ms user=mike.t@example.net"),
    ("checkout", "2026-10-15T19:33:19", "ERROR", "POST /api/checkout/place-order timeout after 5000ms"),
    # auth-service: rotation broke validation. Contains a fake JWT.
    ("auth-service", "2026-10-16T02:01:15", "WARN", "JWKS publish: kid k-2026-10 published, kid k-2026-09 removed"),
    ("auth-service", "2026-10-16T02:03:48", "ERROR",
     "token validation failed: unknown kid 'k-2026-09' token=eyJhbGciOiJSUzI1NiIsImtpZCI6ImstMjAyNi0wOSJ9"
     ".eyJzdWIiOiJ1XzgyMTEiLCJlbWFpbCI6ImphbmVAZXhhbXBsZS5jb20ifQ.c2lnbmF0dXJlLWZha2U"),
    ("auth-service", "2026-10-16T02:06:30", "ERROR",
     "token validation failed: invalid signature for session of user priya.customer@example.com"),
    ("auth-service", "2026-10-16T02:09:02", "ERROR", "token validation failed: unknown kid 'k-2026-09' (x 9812/min)"),
    ("checkout", "2026-10-16T02:10:40", "ERROR", "auth-service rejected session token: 401 unknown kid"),
    # orders-db: replica lag.
    ("orders-db", "2026-10-12T09:06:00", "WARN",
     "orders-db-2: long-running query 340s application_name=analytics-export "
     "SELECT ... FROM orders JOIN payments ... (conn postgres://reporter:Rep0rt!ng2026@orders-db-2:5432/orders)"),
    ("orders-db", "2026-10-12T09:14:30", "WARN", "orders-db-2: replication lag 95s, WAL replay paused on conflict"),
    ("orders-db", "2026-10-12T09:16:45", "ERROR", "order-history: served stale read for order 100479920 (lag 101s)"),
]

PAST_INCIDENTS = [
    ("INC-1987", "payments-api", "P1", "Payments 5xx after release v2.31.0",
     "Card authorizations returned 500 for 22% of requests starting 12 minutes after a payments-api deploy.",
     "Release v2.31.0 dereferenced a missing field in the refund path shared with authorization.",
     "Rolled back to v2.30.4 via Argo CD; errors cleared in 4 minutes. Regression test added.",
     "2026-06-03T10:14", "2026-06-03T10:52"),
    ("INC-2014", "payments-api", "P2", "Processor timeouts during upstream gateway outage",
     "payments-api processor_error_rate rose to 35% with 503 and timeout responses; our 5xx was low.",
     "The external payment processor had a regional outage.",
     "Enabled payments.processor.fallback_queue to queue authorizations; posted status update; no rollback.",
     "2026-07-19T18:40", "2026-07-19T20:05"),
    ("INC-2033", "payments-api", "P2", "payments-api database connection pool exhaustion",
     "Latency climbed and logs showed timeout acquiring connection; db pool at 100%.",
     "A slow reporting query on the orders primary held locks, starving payments-api connections.",
     "Terminated the reporting query and scaled payments-api from 6 to 10 replicas.",
     "2026-08-08T13:02", "2026-08-08T13:41"),
    ("INC-2050", "checkout", "P2", "Checkout latency during Black Friday preview sale",
     "checkout p99 latency reached 5 seconds with CPU above 90% as traffic tripled; no recent deploy.",
     "checkout was under-provisioned for sale traffic; recommendation widget consumed a third of CPU.",
     "Scaled checkout from 8 to 14 replicas and enabled checkout.recommendations.disabled for the sale.",
     "2026-09-12T19:05", "2026-09-12T19:50"),
    ("INC-2061", "checkout", "P1", "Checkout failing due to payments-api errors",
     "checkout place-order errors at 30%, logs showed upstream 502 from payments-api.",
     "A bad payments-api deploy; checkout itself was healthy.",
     "Paged payments; payments-api rolled back. Storefront kept informed.",
     "2026-09-21T15:30", "2026-09-21T16:02"),
    ("INC-1990", "auth-service", "P1", "Logins failing after JWT signing key rotation",
     "Login success dropped to 45% and services rejected tokens with unknown kid right after rotation.",
     "The rotator removed the previous key from JWKS before existing tokens expired.",
     "Re-published the previous signing key to JWKS so both keys validated; fixed rotator overlap window.",
     "2026-06-15T02:04", "2026-06-15T02:37"),
    ("INC-2071", "auth-service", "P2", "Credential stuffing from residential proxies",
     "Failed logins spiked 40x from a small set of ASNs.",
     "Credential stuffing attack using leaked password lists.",
     "Enabled auth.login.strict_rate_limit and paged security on-call; forced resets for 37 accounts.",
     "2026-09-30T22:10", "2026-09-30T23:45"),
    ("INC-2002", "orders-db", "P1", "Orders primary disk full",
     "orders-db-1 disk reached 100%; writes failed for checkout and payments-api.",
     "WAL archiving stalled after a credentials change on the backup bucket.",
     "Failed over to orders-db-2 with Patroni after confirming lag under 5 seconds; fixed archiving.",
     "2026-06-28T04:20", "2026-06-28T05:10"),
    ("INC-2044", "orders-db", "P3", "Replica lag from analytics export query",
     "Replica lag reached 120 seconds; order history served stale data.",
     "A long-running analytics export on orders-db-2 conflicted with WAL replay.",
     "Cancelled the export query; moved analytics exports to a dedicated reporting replica.",
     "2026-08-27T09:00", "2026-08-27T09:35"),
    ("INC-2077", "checkout", "P3", "Order confirmation emails delayed",
     "Confirmation emails delayed by up to 40 minutes; orders completed normally.",
     "The email provider throttled our sending domain.",
     "Raised provider quota; no customer-facing status update needed.",
     "2026-10-02T11:15", "2026-10-02T12:30"),
]


def _metric_rows() -> list[tuple]:
    rows = []
    for start, end in SCENARIO_WINDOWS:
        ts, stop = t(start), t(end)
        while ts <= stop:
            for service, base in HEALTHY.items():
                key = (service, ts.strftime("%Y-%m-%dT%H:%M"))
                rows.append((service, ts, json.dumps({**base, **ANOMALIES.get(key, {})})))
            ts += timedelta(minutes=5)
    return rows


def main() -> None:
    pool = make_pool(get_settings().database_url)
    apply_schema(pool)
    with pool.connection() as conn, conn.transaction():
        conn.execute("TRUNCATE deploys, metric_snapshots, log_snapshots, past_incidents RESTART IDENTITY")
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO deploys (service, version, previous_version, author, commit_sha, summary,"
                " has_migration, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [(*d[:7], t(d[7])) for d in DEPLOYS],
            )
            metrics = _metric_rows()
            cur.executemany(
                "INSERT INTO metric_snapshots (service, captured_at, metrics) VALUES (%s, %s, %s)", metrics
            )
            cur.executemany(
                "INSERT INTO log_snapshots (service, captured_at, level, message) VALUES (%s, %s, %s, %s)",
                [(s, t(ts), lvl, msg) for s, ts, lvl, msg in LOGS],
            )
            cur.executemany(
                "INSERT INTO past_incidents (id, service, severity, title, summary, root_cause, resolution,"
                " started_at, resolved_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [(*p[:7], t(p[7]), t(p[8])) for p in PAST_INCIDENTS],
            )
    pool.close()
    print(
        f"seeded {len(DEPLOYS)} deploys, {len(metrics)} metric snapshots, {len(LOGS)} log lines, "
        f"{len(PAST_INCIDENTS)} past incidents"
    )


if __name__ == "__main__":
    main()
