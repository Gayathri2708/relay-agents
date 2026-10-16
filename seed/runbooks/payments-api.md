# Runbook: payments-api

payments-api authorizes and captures card and wallet payments for checkout. It calls the external processor (Stripe-compatible gateway) and writes payment records to the orders database. Owner: team payments, PagerDuty service PAY. Dashboards: grafana/d/payments-api.

## [PAY-1] Triage checklist
First, check if the error rate rose right after a deploy: compare the alert start time with the last payments-api deploy. Second, check whether errors are 5xx from our service or declines and timeouts from the processor; the metric payments_processor_errors_total splits them. Third, check database connection pool saturation, metric payments_db_pool_in_use. Most payments-api incidents are a bad deploy, a processor outage, or pool exhaustion.

## [PAY-2] Elevated 5xx after a deploy
If 5xx errors started within 30 minutes of a payments-api deploy and the processor error rate is normal, assume the deploy is the cause. Roll back to the previous release following [DEP-2]. Rolling back is a risky action and needs approval from the incident commander for P1 and P2. Do not attempt a forward fix during an active P1.

## [PAY-3] Processor timeouts or outage
If payments_processor_errors_total is elevated with timeout or 503 reasons and our own error rate otherwise looks healthy, check the processor status page. Do not roll back. Enable the circuit breaker flag payments.processor.fallback_queue, which queues authorizations for retry instead of failing them; changing a flag in production needs approval. Draft a status update following [COMM-1] saying card payments may be delayed.

## [PAY-4] Database connection pool exhaustion
Symptoms: payments_db_pool_in_use at 100%, latency rising, logs show "timeout acquiring connection". Usually caused by slow queries on the orders database or a traffic spike. Check orders database health first ([DB-1]). If the database is healthy, scale payments-api out from 6 to 10 replicas; each replica opens 20 connections, so confirm the database has headroom (max_connections 500). Scaling is a risky action and needs approval.

## [PAY-5] Who to page
Page team payments (PAY) for P1 and P2. If the processor is at fault, also ask the payments lead to open a ticket with the processor. Escalate to data-platform (DBA) if the root cause is in the orders database.
