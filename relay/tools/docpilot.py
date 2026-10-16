"""Client for DocPilot (github.com/Gayathri2708/docpilot-rag): runbook search and grounded Q&A."""

from dataclasses import dataclass

import httpx

from ..config import Settings


class DocPilotError(Exception):
    pass


@dataclass(frozen=True)
class RunbookHit:
    chunk_id: int
    filename: str
    content: str
    score: float


@dataclass(frozen=True)
class RunbookAnswer:
    answer: str
    answered: bool
    sources: list[str]  # filenames DocPilot cited


class DocPilotClient:
    def __init__(self, base_url: str, api_key: str, timeout_s: float = 60.0, transport=None):
        if not api_key:
            raise DocPilotError("DOCPILOT_API_KEY is not set (see .env.example).")
        self._http = httpx.Client(
            base_url=base_url, headers={"X-API-Key": api_key}, timeout=timeout_s, transport=transport
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> "DocPilotClient":
        return cls(settings.docpilot_url, settings.docpilot_api_key, settings.docpilot_timeout_s)

    def _post(self, path: str, body: dict) -> object:
        try:
            resp = self._http.post(path, json=body)
        except httpx.HTTPError as exc:
            raise DocPilotError(f"DocPilot unreachable: {exc}") from exc
        if resp.status_code >= 400:
            raise DocPilotError(f"DocPilot {path} returned {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def search(self, query: str, top_k: int = 5, mode: str | None = None) -> list[RunbookHit]:
        body: dict = {"query": query, "top_k": top_k}
        if mode:
            body["mode"] = mode
        hits = self._post("/search", body)
        return [
            RunbookHit(int(h["chunk_id"]), h["filename"], h["content"], float(h["score"]))
            for h in hits  # type: ignore[union-attr]
        ]

    def ask(self, question: str, top_k: int = 5) -> RunbookAnswer:
        data = self._post("/ask", {"question": question, "top_k": top_k})
        return RunbookAnswer(
            answer=data["answer"],  # type: ignore[index]
            answered=bool(data["answered"]),  # type: ignore[index]
            sources=sorted({s["filename"] for s in data["sources"] if s.get("cited")}),  # type: ignore[index]
        )
