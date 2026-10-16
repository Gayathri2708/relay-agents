import ast
from pathlib import Path

import pytest

import relay.executor
from relay.executor import MockExecutor, NotApproved
from relay.schemas import ProposedAction

ACTION = ProposedAction(
    id="A1", kind="rollback", title="Roll back payments-api", details="Roll back to v2.40.3 per DEP-2.",
    target="payments-api", citations=[{"runbook": "deploys-and-rollback.md", "section": "DEP-2"}],
)


def test_simulates_an_approved_action():
    sim = MockExecutor().simulate(ACTION, "approved")
    assert sim.simulated is True and sim.executed is False
    assert all(line.startswith("[dry run]") for line in sim.transcript)
    assert any("argocd app rollback payments-api" in line for line in sim.transcript)
    assert "nothing was executed" in sim.transcript[-1]


@pytest.mark.parametrize("decision", ["rejected", "pending", ""])
def test_refuses_anything_not_approved(decision):
    with pytest.raises(NotApproved):
        MockExecutor().simulate(ACTION, decision)


def test_every_action_kind_has_a_simulation():
    from typing import get_args

    from relay.schemas import ActionKind

    for kind in get_args(ActionKind):
        action = ACTION.model_copy(update={"kind": kind, "status_update_draft": "We are investigating."})
        assert MockExecutor().simulate(action, "approved").transcript


def test_executor_module_cannot_touch_the_outside_world():
    tree = ast.parse(Path(relay.executor.__file__).read_text())
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")])
    }
    forbidden = {"subprocess", "os", "socket", "httpx", "requests", "urllib", "shutil", "openai", "psycopg"}
    assert not imported & forbidden, imported & forbidden
    calls = {n.func.id for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert not calls & {"eval", "exec", "__import__", "open"}
