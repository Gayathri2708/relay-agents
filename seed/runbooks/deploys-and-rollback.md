# Runbook: deploys and rollback

All Brightcart services deploy through Argo CD from the main branch. Every deploy records service, version, author, commit and time in the deploys table, which the incident tooling can query.

## [DEP-1] Is it the deploy?
A deploy is the likely cause if symptoms began within 30 minutes after it finished and no dependency is failing. Compare the error start time with the deploy finish time, and check whether the deploy touched the failing code path. A deploy more than 2 hours before the incident is rarely the cause.

## [DEP-2] Rollback procedure
Rollback is a risky action and needs incident commander approval for P1 and P2. Steps: in Argo CD, select the service, choose History and Rollback, pick the previous healthy version; or run argocd app rollback SERVICE PREVIOUS_REVISION. Watch the error rate for 10 minutes. Freeze further deploys to that service with the deploy-freeze label until the incident is resolved.

## [DEP-3] Migrations
If the bad deploy included a database migration, do not roll back the application without checking that the previous version works with the new schema. Page data-platform (DBA) before rolling back across a migration.

## [DEP-4] After the rollback
Open a ticket for the owning team to find the root cause, link the incident, and add a regression test before redeploying.
