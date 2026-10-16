# Brightcart Incident Severity and Communication Policy

Owner: SRE team. Applies to every production incident at Brightcart. When in doubt, pick the higher severity; downgrading later is cheap, under-reacting is not.

## [SEV-1] P1 Critical
P1 means customers cannot complete a purchase, cannot log in, or card data or credentials may be exposed. Examples: checkout error rate above 25% for 5 minutes, payments-api authorizations failing above 10%, auth-service login success below 80%, primary orders database unavailable, any confirmed security breach. Response: page the owning team and the incident commander immediately, open a war-room channel, first status page update within 15 minutes, updates every 30 minutes.

## [SEV-2] P2 High
P2 means a core flow is degraded but still mostly working, or a single region or payment method is down. Examples: checkout p99 latency above 3 seconds, payments-api error rate between 2% and 10%, one card network or wallet failing, database replica lag above 60 seconds, login success between 80% and 95%. Response: page the owning team, incident commander optional, status page update within 30 minutes if customers notice, updates every hour.

## [SEV-3] P3 Moderate
P3 means a non-critical feature is broken or a core flow shows a small, contained error increase with a workaround. Examples: order confirmation emails delayed, error rate between 0.5% and 2%, a single noisy pod restarting. Response: notify the owning team in their channel during business hours. No status page update unless customers report it.

## [SEV-4] P4 Low
P4 means no customer impact: a warning-level alert, capacity trending toward a limit, a flaky internal job. Response: create a ticket for the owning team. No page.

## [SEV-5] Escalation and downgrade rules
Escalate one level if impact grows, if the incident lasts longer than 30 minutes at P2, or if revenue loss is confirmed. Any suspected leak of card data, secrets or customer personal data is P1 regardless of traffic impact and must also page the security on-call. Only the incident commander may downgrade a P1 or P2.

## [COMM-1] Status page updates
Customer-facing updates go on status.brightcart.example. Never name internal services, hosts, or people. Use the template: "We are investigating reports of [symptom customers see]. [What still works]. Next update by [time UTC]." Every status update is drafted by the responder and approved by the incident commander before publishing.

## [COMM-2] Internal communication
Post a summary in #incidents with severity, owning team, start time, current hypothesis and the next decision point. Tag the incident commander for P1 and P2. Record every action taken, by whom and when, in the incident timeline.

## [COMM-3] Paging and escalation contacts
Owning teams: payments-api is owned by team payments (PagerDuty service PAY), checkout by team storefront (CHK), auth-service by team identity (IDN), orders database by team data-platform (DBA). The incident commander rotation is PagerDuty schedule IC. The security on-call is PagerDuty schedule SEC.
