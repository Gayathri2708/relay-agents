"""OpsData backed by one eval incident's own fixture data (no database)."""

import re
from datetime import datetime, timedelta

from relay.tools.ops import Deploy, LogLine, MetricSnapshot, PastIncident
from seed.seed_db import PAST_INCIDENTS, t

_PAST = [
    PastIncident(id=p[0], service=p[1], severity=p[2], title=p[3], summary=p[4], root_cause=p[5],
                 resolution=p[6], started_at=t(p[7]))
    for p in PAST_INCIDENTS
]
_WORD = re.compile(r"[a-z][a-z0-9]{2,}")


class FixtureOps:
    def __init__(self, case: dict):
        self.start = datetime.fromisoformat(case["start"].replace("Z", "+00:00"))
        self.ops = case["ops"]

    def _at(self, offset_min: float) -> datetime:
        return self.start + timedelta(minutes=offset_min)

    def recent_deploys(self, at, lookback=timedelta(hours=6)) -> list[Deploy]:
        deploys = [
            Deploy(**{k: v for k, v in d.items() if k != "minutes_before"}, finished_at=self._at(-d["minutes_before"]))
            for d in self.ops["deploys"]
        ]
        return sorted(
            (d for d in deploys if at - lookback <= d.finished_at <= at + timedelta(minutes=15)),
            key=lambda d: d.finished_at,
            reverse=True,
        )

    def metrics(self, service, at) -> list[MetricSnapshot]:
        return [
            MetricSnapshot(service=service, captured_at=self._at(m["offset_min"]), metrics=m["metrics"])
            for m in self.ops["metrics"].get(service, [])
        ]

    def logs(self, service, at, limit=15) -> list[LogLine]:
        return [
            LogLine(service=service, captured_at=self._at(line["offset_min"]), level=line["level"],
                    message=line["message"])
            for line in self.ops["logs"]
            if line["service"] == service and line["level"] in ("ERROR", "WARN")
        ][:limit]

    def similar_incidents(self, service, text, limit=3) -> list[PastIncident]:
        words = set(_WORD.findall(text.lower()))

        def score(p: PastIncident) -> float:
            overlap = len(words & set(_WORD.findall(f"{p.title} {p.summary} {p.root_cause}".lower())))
            return overlap * (2 if p.service == service else 1)

        ranked = sorted(_PAST, key=score, reverse=True)
        return [p for p in ranked if score(p) > 0][:limit]
