# Runbook: checkout

checkout is the storefront service that runs cart, shipping, tax and order placement. It depends on payments-api for payment, auth-service for sessions, and the orders database. Owner: team storefront, PagerDuty service CHK.

## [CHK-1] Triage checklist
A checkout alert is often a symptom of a dependency. Check, in order: payments-api error rate, auth-service login and token validation, orders database health, and the most recent checkout deploy. If a dependency is failing, follow that runbook and page that team instead of storefront.

## [CHK-2] Checkout errors caused by payments-api
If checkout logs show "payment authorization failed" or upstream 502 from payments-api, the incident belongs to payments. Follow [PAY-1] and page team payments. Keep storefront informed in #incidents.

## [CHK-3] Bad checkout deploy
If checkout errors or latency started within 30 minutes of a checkout deploy and dependencies look healthy, roll back checkout to the previous release following [DEP-2]. Rollback needs incident commander approval for P1 and P2.

## [CHK-4] Latency and saturation
If checkout p99 latency is above 3 seconds with high CPU (above 85%) and no recent deploy, the service is likely under-provisioned for traffic, for example during a sale. Scale checkout from 8 to 14 replicas. Scaling needs approval. Consider enabling the flag checkout.recommendations.disabled to shed the recommendation widget, which costs about 30% of checkout CPU.

## [CHK-5] Customer communication
For checkout incidents at P1 or P2, draft a status page update using [COMM-1]: "Some customers may be unable to complete purchases. Browsing and carts are unaffected."
