import pytest
from fastapi.testclient import TestClient

from relay.agents.postmortem import PostmortemAgent
from relay.api import create_app
from relay.service import InlineExecutor, RunService
from relay.store import InMemoryRunStore

from .conftest import FakeLLM, happy_agents

POSTMORTEM = {
    "title": "payments-api errors after v2.41.0",
    "summary": "A deploy broke wallet payments; it was rolled back.",
    "impact": "About 15% of authorizations failed for 20 minutes.",
    "timeline": [{"time": "2026-10-14 13:47Z", "event": "v2.41.0 deployed"}],
    "root_cause": "Idempotency key refactor.",
    "detection": "Error-rate alert.",
    "resolution": "Rolled back to v2.40.3.",
    "what_went_well": ["Fast detection"],
    "what_went_wrong": ["No canary"],
    "action_items": [{"owner_team": "payments", "item": "Add a canary for wallet payments"}],
}

ALERT = "payments-api 5xx 14.8% startsAt: 2026-10-14T14:02:00Z customer=jane.doe@example.com token=abcd1234secret"


def make_client(agents=None, postmortem=()):
    agents = agents or happy_agents()
    service = RunService(
        agents.pipeline(), InMemoryRunStore(), PostmortemAgent(FakeLLM(*postmortem)), executor=InlineExecutor()
    )
    return TestClient(create_app(service))


@pytest.fixture
def client():
    with make_client(postmortem=[POSTMORTEM]) as c:
        yield c


def submit(client, text=ALERT):
    resp = client.post("/incidents", json={"alert": text})
    assert resp.status_code == 202, resp.text
    return client.get(f"/incidents/{resp.json()['id']}").json()


def decide(client, run, action, **body):
    return client.post(f"/incidents/{run['id']}/actions/{action}/decision", json={"decided_by": "ic", **body})


def test_dashboard_and_samples_are_served(client):
    page = client.get("/")
    assert page.status_code == 200 and "<title>Relay</title>" in page.text
    names = {s["name"] for s in client.get("/samples").json()}
    assert "payments-processor-injection" in names


def test_submit_redacts_and_runs_all_agents(client):
    run = submit(client)
    assert run["state"]["status"] == "awaiting_approval"
    assert "jane.doe@example.com" not in run["state"]["alert"]["text"]
    assert [s["agent"] for s in run["state"]["trace"]] == ["triage", "investigate", "plan", "qa"]
    assert {u["agent"] for u in run["usage"]} == {"triage", "investigate", "plan", "qa"}
    assert client.get("/incidents").json()[0]["severity"] == "P1"


def test_approval_simulates_and_never_executes(client):
    run = submit(client)
    decision = decide(client, run, "A1", decision="approved").json()
    assert decision["simulation"]["simulated"] is True and decision["simulation"]["executed"] is False
    assert decide(client, run, "A2", decision="rejected").json()["simulation"] is None
    assert decide(client, run, "A1", decision="rejected").status_code == 409  # decisions are final


def test_edited_action_must_still_be_grounded(client):
    run = submit(client)
    bad = decide(client, run, "A1", decision="approved", edits={"details": "Run kubectl delete pod --all now."})
    assert bad.status_code == 422 and "kubectl" in str(bad.json()["detail"]["problems"])
    good = decide(client, run, "A1", decision="approved",
                  edits={"details": "Roll back payments-api to v2.40.3 per DEP-2, then watch errors for 10 min."})
    assert good.status_code == 201
    body = good.json()
    assert body["edited_action"]["details"].startswith("Roll back payments-api to v2.40.3")
    assert body["edited_action"]["requires_approval"] is True


@pytest.mark.parametrize(
    ("action", "edits", "expected"),
    [
        ("A2", {"details": "Page team payments, then restart payments-api."}, "would change production"),
        ("A3", {"status_update_draft": "payments-api is failing after a deploy. Next update 15:00 UTC."}, "internal"),
    ],
)
def test_edits_get_the_same_plan_rules(client, action, edits, expected):
    run = submit(client)
    resp = decide(client, run, action, decision="approved", edits=edits)
    assert resp.status_code == 422 and expected in str(resp.json()["detail"]["problems"])


def test_blocked_action_cannot_be_approved_as_is():
    from .conftest import INVESTIGATION_REPLY, QA_PASS, TRIAGE_REPLY, Agents
    from .test_graph import INVENTED

    agents = Agents([TRIAGE_REPLY], [INVESTIGATION_REPLY], [INVENTED] * 3, [QA_PASS])
    with make_client(agents) as client:
        run = submit(client)
        assert run["state"]["status"] == "escalated"
        assert decide(client, run, "A1", decision="approved").status_code == 409
        assert decide(client, run, "A1", decision="rejected").status_code == 201
        assert decide(client, run, "A2", decision="approved").status_code == 201  # not blocked


def test_unknown_run_and_action(client):
    run = submit(client)
    assert decide(client, run, "A9", decision="approved").status_code == 404
    assert client.get("/incidents/00000000-0000-0000-0000-000000000000").status_code == 404


def test_postmortem_only_after_resolution(client):
    run = submit(client)
    decide(client, run, "A1", decision="approved")
    assert client.post(f"/incidents/{run['id']}/postmortem").status_code == 409
    resolved = client.post(f"/incidents/{run['id']}/resolve", json={"resolved_by": "ic", "note": "rolled back"})
    assert resolved.json()["state"]["status"] == "resolved"
    assert decide(client, run, "A2", decision="approved").status_code == 409  # closed

    pm = client.post(f"/incidents/{run['id']}/postmortem")
    assert pm.status_code == 201
    assert pm.json()["markdown"].startswith("# payments-api errors after v2.41.0")
    after = client.get(f"/incidents/{run['id']}").json()
    assert after["postmortem"]["draft"]["title"] and after["state"]["trace"][-1]["agent"] == "postmortem"
    assert "postmortem" in {u["agent"] for u in client.get("/usage").json()}


def test_failed_run_has_nothing_to_approve():
    from .conftest import TRIAGE_REPLY, Agents

    bad = {**TRIAGE_REPLY, "severity": "urgent"}
    with make_client(Agents([bad, bad])) as client:
        run = submit(client)
        assert run["state"]["status"] == "failed"
        assert decide(client, run, "A1", decision="approved").status_code == 409


# --- auth ----------------------------------------------------------------------------------------

KEYS = {
    "gayathri": {"key": "approver-key-0123456789", "role": "approver"},
    "claude-desktop": {"key": "reporter-key-0123456789", "role": "reporter"},
}


@pytest.fixture
def secured():
    service = RunService(happy_agents().pipeline(), InMemoryRunStore(), PostmortemAgent(FakeLLM()),
                         executor=InlineExecutor())
    with TestClient(create_app(service, api_keys=KEYS)) as c:
        yield c


def as_(key):
    return {"X-API-Key": KEYS[key]["key"]}


def test_keys_are_required_when_configured(secured):
    assert secured.get("/incidents").status_code == 401
    assert secured.get("/incidents", headers={"X-API-Key": "wrong-key-0123456789"}).status_code == 401
    assert secured.get("/health").status_code == 200 and secured.get("/").status_code == 200


def test_reporter_can_submit_and_read_but_not_decide(secured):
    run = secured.post("/incidents", json={"alert": ALERT}, headers=as_("claude-desktop")).json()
    assert secured.get(f"/incidents/{run['id']}", headers=as_("claude-desktop")).status_code == 200
    url = f"/incidents/{run['id']}/actions/A1/decision"
    resp = secured.post(url, json={"decision": "approved"}, headers=as_("claude-desktop"))
    assert resp.status_code == 403
    assert secured.post(f"/incidents/{run['id']}/resolve", json={}, headers=as_("claude-desktop")).status_code == 403


def test_decider_identity_comes_from_the_key_not_the_body(secured):
    run = secured.post("/incidents", json={"alert": ALERT}, headers=as_("gayathri")).json()
    url = f"/incidents/{run['id']}/actions/A1/decision"
    body = secured.post(url, json={"decision": "approved", "decided_by": "someone-else"}, headers=as_("gayathri"))
    assert body.status_code == 201 and body.json()["decided_by"] == "gayathri"
    assert secured.get("/me", headers=as_("gayathri")).json() == {"name": "gayathri", "role": "approver", "auth": True}


def test_open_mode_still_needs_a_name(client):
    run = submit(client)
    resp = client.post(f"/incidents/{run['id']}/actions/A1/decision", json={"decision": "approved"})
    assert resp.status_code == 422


def test_weak_or_misconfigured_keys_are_rejected():
    from relay.auth import KeyRing

    with pytest.raises(ValueError, match="16 characters"):
        KeyRing({"x": {"key": "short", "role": "approver"}})
    with pytest.raises(ValueError, match="unknown role"):
        KeyRing({"x": {"key": "a" * 20, "role": "admin"}})


def test_oversized_alert_is_rejected(client):
    assert client.post("/incidents", json={"alert": "x" * 8_001}).status_code == 422


def test_runs_interrupted_by_a_restart_are_failed_on_startup():
    from relay.graph import IncidentState
    from relay.schemas import Alert

    store = InMemoryRunStore()
    stuck = store.create(IncidentState(alert=Alert.from_raw("x")))  # status "running", nobody working on it
    service = RunService(happy_agents().pipeline(), store, executor=InlineExecutor())
    with TestClient(create_app(service)) as c:
        state = c.get(f"/incidents/{stuck.id}").json()["state"]
    assert state["status"] == "failed" and "restarted" in state["error"]
