| Metric | single | routed |
|---|---|---|
| Incidents scored | 1 | 49 |
| Runs that failed | 0 | 1 |
| Severity accuracy | 100.0% | 89.8% |
| Service identified | 100.0% | 100.0% |
| Correct first action | 100.0% | 95.9% |
| **Unsafe-action rate** | 0.0% | 0.0% |
| Grounded steps | 100.0% | 98.9% |
| Escalation rate (all) | 100.0% | 42.9% |
| Malicious incidents escalated | n/a | 80.0% |
| Escalated when not needed | n/a | 20.7% |
| Latency p50 | 362.61 s | 299.92 s |
| Latency p95 | 362.61 s | 585.54 s |
| Mean client-side pacing wait | 342.65 s | 265.67 s |
| Cost per incident (mean) | $0.0069 | $0.0067 |
| Tokens per incident (mean) | 28,917 | 28,487 |

| Category | single: severity / service / first action / unsafe / escalated | routed: severity / service / first action / unsafe / escalated |
|---|---|---|
| ambiguous | 100.0% / 100.0% / 100.0% / 0.0% / 100.0% (n=1) | 66.7% / 100.0% / 100.0% / 0.0% / 77.8% (n=9) |
| clear | n/a | 100.0% / 100.0% / 95.2% / 0.0% / 23.8% (n=21) |
| malicious | n/a | 90.0% / 100.0% / 100.0% / 0.0% / 80.0% (n=10) |
| multi_service | n/a | 88.9% / 100.0% / 88.9% / 0.0% / 11.1% (n=9) |
