"""Registro y ejecución del incremento 4 sobre piezas existentes.

Registro: ``workspace_for`` del backend sobre un clon real en ``tmp_path``
(``MACAE_WORKSPACE_ROOT`` apuntado ahí); sólo ``docs/incidents/*.json`` con
``incident_id`` cuentan. Ejecución: ``call_tool("workspace_exec", ...)`` con
un doble que devuelve el JSON exacto de ``format_success_response``; exit≠0
es evidencia, ``status: error`` es fallo de capacidad. Sin referencia durable
en config no hay capacidad.
"""

import json
import subprocess

import pytest

import v4.common.services.workspace_service as ws_mod
import v4.control.workspace_capability as wc
from v4.control.workspace_capability import WorkspaceCapability, from_config

INC = {
    "incident_id": "INC-2026-004",
    "authority_ceiling": {"max_action_class_without_human": "read-only"},
    "learn": {
        "executable_probe": {
            "class": "read-only",
            "cwd": ".",
            "command_or_test": "true",
        }
    },
}


class FakeTool:
    def __init__(self):
        self.calls = []

    async def call_tool(self, tool_name, **kwargs):
        self.calls.append((tool_name, kwargs))
        if kwargs["command"] == "boom":
            # Texto exacto de format_error_response("Workspace not found") en ca-mcp.
            return (
                "##### ❌ Error\n\n**Error:** Workspace not found\n\n"
                "AGENT SUMMARY: An error occurred while processing the request."
            )
        return json.dumps(
            {
                "status": "success",
                "action": tool_name,
                "details": {
                    "command": kwargs["command"],
                    "cwd": kwargs["path"] or "/",
                    "exit_code": 3,
                    "stdout": "out",
                    "stderr": "err",
                    "truncated": False,
                },
            }
        )

    async def close(self):
        pass


@pytest.fixture
def clone(tmp_path, monkeypatch):
    root = tmp_path / "workspaces"
    monkeypatch.setattr(ws_mod, "WORKSPACE_ROOT", root)
    ws = root / "reg-user" / "macae-clone"
    (ws / "docs" / "incidents" / "probes").mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(ws)], check=True)
    (ws / "docs" / "incidents" / "INC-2026-004.x.json").write_text(json.dumps(INC))
    (ws / "docs" / "incidents" / "README.md").write_text("no")
    (ws / "docs" / "incidents" / "schema.json").write_text(json.dumps({"$schema": "x"}))
    return ws


@pytest.fixture
def cap(clone):
    return WorkspaceCapability(
        user_id="reg-user", workspace_id="macae-clone", tool=FakeTool()
    )


@pytest.mark.asyncio
async def test_registry_reads_incidents_from_the_clone_on_the_share(cap):
    assert [i["incident_id"] for i in await cap.registry()] == ["INC-2026-004"]


@pytest.mark.asyncio
async def test_execute_goes_through_workspace_exec_and_returns_evidence_verbatim(cap):
    evidence = await cap.execute("uv run pytest -q", "src/backend")
    assert (evidence.exit_code, evidence.stdout, evidence.stderr) == (3, "out", "err")
    name, args = cap._tool.calls[-1]
    assert name == "workspace_exec"
    assert args == {
        "user_id": "reg-user",
        "workspace_id": "macae-clone",
        "command": "uv run pytest -q",
        "path": "src/backend",
        "timeout": wc.EXEC_TIMEOUT_SECONDS,
    }
    await cap.execute("true", ".")
    assert cap._tool.calls[-1][1]["path"] == ""


@pytest.mark.asyncio
async def test_tool_error_is_a_capability_failure_not_evidence(cap):
    with pytest.raises(RuntimeError, match="Workspace not found"):
        await cap.execute("boom", ".")


def test_from_config_requires_the_durable_reference(monkeypatch):
    monkeypatch.setattr(wc.config, "INCIDENT_REGISTRY_USER_ID", None, raising=False)
    monkeypatch.setattr(
        wc.config, "INCIDENT_REGISTRY_WORKSPACE_ID", "ws", raising=False
    )
    monkeypatch.setattr(
        wc.config, "MCP_SERVER_ENDPOINT", "http://mcp/mcp", raising=False
    )
    assert from_config() is None
    monkeypatch.setattr(wc.config, "INCIDENT_REGISTRY_USER_ID", "u", raising=False)
    cap = from_config()
    assert cap is not None and (cap.user_id, cap.workspace_id) == ("u", "ws")
