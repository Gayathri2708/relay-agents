"""Upload seed/runbooks/*.md into DocPilot's "engineering" access group.

Needs an admin key that can write to engineering (DOCPILOT_ADMIN_KEY). Re-running is safe:
by default a runbook whose filename is already in DocPilot is deleted and uploaded again, so edits land.

    python -m seed.upload_runbooks            # replace existing copies
    python -m seed.upload_runbooks --keep     # skip runbooks that are already uploaded
"""

import argparse
import sys
from pathlib import Path

import httpx

from relay.config import get_settings

RUNBOOK_DIR = Path(__file__).parent / "runbooks"
GROUP = "engineering"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="skip runbooks already in DocPilot")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.docpilot_admin_key:
        print("DOCPILOT_ADMIN_KEY is not set (see .env.example).", file=sys.stderr)
        return 1

    headers = {"X-API-Key": settings.docpilot_admin_key}
    with httpx.Client(base_url=settings.docpilot_url, headers=headers, timeout=120) as client:
        resp = client.get("/documents")
        resp.raise_for_status()
        existing = {d["filename"]: d for d in resp.json() if d.get("access_group") == GROUP}

        for path in sorted(RUNBOOK_DIR.glob("*.md")):
            if path.name in existing:
                if args.keep:
                    print(f"skip     {path.name} (already uploaded)")
                    continue
                client.delete(f"/documents/{existing[path.name]['id']}").raise_for_status()
            with path.open("rb") as fh:
                resp = client.post(
                    "/documents",
                    files={"file": (path.name, fh, "text/markdown")},
                    data={"access_group": GROUP},
                )
            if resp.status_code >= 400:
                print(f"FAILED   {path.name}: {resp.status_code} {resp.text}", file=sys.stderr)
                return 1
            doc = resp.json()
            print(f"uploaded {path.name}: {doc['num_chunks']} chunks -> {GROUP}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
