| Metric | single | routed |
|---|---|---|
| Incidents scored | 11 | 49 |
| Runs that failed | 0 | 1 |
| Severity accuracy | 90.9% | 89.8% |
| Service identified | 100.0% | 100.0% |
| Correct first action | 100.0% | 95.9% |
| **Unsafe-action rate** | 0.0% | 0.0% |
| Grounded steps | 100.0% | 98.9% |
| Escalation rate (all) | 54.5% | 42.9% |
| Malicious incidents escalated | 100.0% | 80.0% |
| Escalated when not needed | 0.0% | 20.7% |
| Latency p50 | 299.48 s | 299.92 s |
| Latency p95 | 541.5 s | 585.54 s |
| Mean client-side pacing wait | 281.45 s | 265.67 s |
| Cost per incident (mean) | $0.0060 | $0.0067 |
| Tokens per incident (mean) | 24,961 | 28,487 |

| Category | single: severity / service / first action / unsafe / escalated | routed: severity / service / first action / unsafe / escalated |
|---|---|---|
| ambiguous | 100.0% / 100.0% / 100.0% / 0.0% / 100.0% (n=3) | 66.7% / 100.0% / 100.0% / 0.0% / 77.8% (n=9) |
| clear | 100.0% / 100.0% / 100.0% / 0.0% / 0.0% (n=3) | 100.0% / 100.0% / 95.2% / 0.0% / 23.8% (n=21) |
| malicious | 66.7% / 100.0% / 100.0% / 0.0% / 100.0% (n=3) | 90.0% / 100.0% / 100.0% / 0.0% / 80.0% (n=10) |
| multi_service | 100.0% / 100.0% / 100.0% / 0.0% / 0.0% (n=2) | 88.9% / 100.0% / 88.9% / 0.0% / 11.1% (n=9) |
