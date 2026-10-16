import pytest
from fastapi.testclient import TestClient

from relay.api import create_app
from relay.graph import IncidentState
from relay.schemas import ResponsePlan, TriageResult
from relay.store import InMemoryRunStore

from .conftest import PLAN_REPLY, TRIAGE_REPLY


class StubPipeline:
    def __init__(self, fail: bool = False):
        self.fail, self.alerts = fail, []

    def run(self, alert):
        self.alerts.append(alert)
        if self.fail:
            return IncidentState(alert=alert, status="failed", failed_stage="triage", error="boom")
        actions = [{"id": f"A{i}", **a} for i, a in enumerate(PLAN_REPLY["actions"], 1)]
        return IncidentState(
            alert=alert,
            triage=TriageResult.model_validate(TRIAGE_REPLY),
            plan=ResponsePlan(summary=PLAN_REPLY["summary"], actions=actions),
            status="awaiting_approval",
        )


@pytest.fixture
def stub():
    return StubPipeline()


@pytest.fixture
def client(stub):
    with TestClient(create_app(stub, InMemoryRunStore())) as c:
        yield c


def submit(client, text="payments-api 5xx, customer=jane.doe@example.com token=abcd1234secret"):
    resp = client.post("/incidents", json={"alert": text})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_submit_redacts_before_pipeline_and_storage(client, stub):
    run = submit(client)
    assert "jane.doe@example.com" not in stub.alerts[0].text
    assert "abcd1234secret" not in run["state"]["alert"]["text"]
    assert run["state"]["alert"]["redactions"] == {"email": 1, "secret_assignment": 1}
    assert run["state"]["plan"]["executed"] is False


def test_list_and_get(client):
    run = submit(client)
    listed = client.get("/incidents").json()
    assert listed[0] == {**listed[0], "id": run["id"], "service": "payments-api", "severity": "P1"}
    assert client.get(f"/incidents/{run['id']}").json()["id"] == run["id"]
    assert client.get("/incidents/00000000-0000-0000-0000-000000000000").status_code == 404


def test_approving_an_action_only_records_it(client):
    run = submit(client)
    resp = client.post(
        f"/incidents/{run['id']}/actions/A1/decision", json={"decision": "approved", "decided_by": "ic@oncall"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["executed"] is False and "never executes" in body["message"]
    decisions = client.get(f"/incidents/{run['id']}").json()["decisions"]
    assert [(d["action_id"], d["decision"]) for d in decisions] == [("A1", "approved")]


def test_decisions_are_final(client):
    run = submit(client)
    url = f"/incidents/{run['id']}/actions/A1/decision"
    assert client.post(url, json={"decision": "rejected", "decided_by": "ic"}).status_code == 201
    assert client.post(url, json={"decision": "approved", "decided_by": "ic"}).status_code == 409


def test_unknown_action_is_404(client):
    run = submit(client)
    resp = client.post(f"/incidents/{run['id']}/actions/A9/decision", json={"decision": "approved", "decided_by": "x"})
    assert resp.status_code == 404


def test_failed_run_has_nothing_to_approve():
    with TestClient(create_app(StubPipeline(fail=True), InMemoryRunStore())) as client:
        run = submit(client)
        assert run["state"]["status"] == "failed"
        resp = client.post(
            f"/incidents/{run['id']}/actions/A1/decision", json={"decision": "approved", "decided_by": "x"}
        )
        assert resp.status_code == 409
