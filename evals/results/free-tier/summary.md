| Metric | single | routed |
|---|---|---|
| Incidents scored | 20 | 49 |
| Runs that failed | 1 | 1 |
| Severity accuracy | 90.0% | 89.8% |
| Service identified | 100.0% | 100.0% |
| Correct first action | 95.0% | 95.9% |
| **Unsafe-action rate** | 0.0% | 0.0% |
| Grounded steps | 100.0% | 98.9% |
| Escalation rate (all) | 40.0% | 42.9% |
| Malicious incidents escalated | 80.0% | 80.0% |
| Escalated when not needed | 0.0% | 20.7% |
| Latency p50 | 301.06 s | 299.92 s |
| Latency p95 | 554.33 s | 585.54 s |
| Mean client-side pacing wait | 275.08 s | 265.67 s |
| Cost per incident (mean) | $0.0057 | $0.0067 |
| Tokens per incident (mean) | 23,831 | 28,487 |

| Category | single: severity / service / first action / unsafe / escalated | routed: severity / service / first action / unsafe / escalated |
|---|---|---|
| ambiguous | 80.0% / 100.0% / 100.0% / 0.0% / 80.0% (n=5) | 66.7% / 100.0% / 100.0% / 0.0% / 77.8% (n=9) |
| clear | 100.0% / 100.0% / 100.0% / 0.0% / 0.0% (n=5) | 100.0% / 100.0% / 95.2% / 0.0% / 23.8% (n=21) |
| malicious | 80.0% / 100.0% / 100.0% / 0.0% / 80.0% (n=5) | 90.0% / 100.0% / 100.0% / 0.0% / 80.0% (n=10) |
| multi_service | 100.0% / 100.0% / 80.0% / 0.0% / 0.0% (n=5) | 88.9% / 100.0% / 88.9% / 0.0% / 11.1% (n=9) |
