"""El listado de workspaces dice lo que el usuario necesita ver, sin leer logs.

Rama actual, si el workspace es el registro de incidentes y si el reconciliador
lo mantiene al día. Repo de verdad en ``tmp_path``; nada de dobles de git.
"""

import json
import subprocess
from pathlib import Path

import pytest

import v4.api.workspace_router as wr
import v4.common.services.workspace_service as ws_mod
from v4.common.services.workspace_service import REGISTRY_WORKSPACE_ID

USER = "listing-user"


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


class Req:
    headers = {"x-ms-client-principal-id": USER}


def make(root: Path, workspace_id: str, *, branch: str, registry: bool) -> Path:
    ws = root / USER / workspace_id
    ws.mkdir(parents=True)
    git(ws, "init", "-q", "-b", branch)
    git(ws, "config", "user.email", "t@t")
    git(ws, "config", "user.name", "t")
    (ws / ws_mod._META_FILE).write_text(json.dumps({"name": workspace_id}))
    if registry:
        (ws / "docs" / "incidents").mkdir(parents=True)
        (ws / "docs" / "incidents" / "INC-2026-004.x.json").write_text("{}")
    (ws / "archivo.txt").write_text("x")
    git(ws, "add", "-A")
    git(ws, "commit", "-qm", "inicial")
    return ws


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    monkeypatch.setattr(ws_mod, "WORKSPACE_ROOT", base)
    monkeypatch.setattr(wr, "_auth_user", lambda request: USER)
    return base


def by_id(response):
    return {w.workspace_id: w for w in response.workspaces}


def test_listing_shows_the_branch_of_each_workspace(root):
    make(root, "mi-ws", branch="stable/v4-baseline", registry=False)

    assert by_id(wr.list_workspaces(Req()))["mi-ws"].branch == "stable/v4-baseline"


def test_the_users_workspace_with_the_registry_is_marked_but_not_owned(root):
    """Tiene docs/incidents, así que se lee; pero nadie le hace merge por debajo."""
    make(root, "mi-repo", branch="main", registry=True)

    summary = by_id(wr.list_workspaces(Req()))["mi-repo"]

    assert summary.is_incident_registry is True
    assert summary.reconciler_owned is False


def test_the_reconcilers_own_workspace_is_marked_as_owned(root):
    make(root, REGISTRY_WORKSPACE_ID, branch="main", registry=True)

    summary = by_id(wr.list_workspaces(Req()))[REGISTRY_WORKSPACE_ID]

    assert summary.is_incident_registry is True
    assert summary.reconciler_owned is True


def test_a_workspace_without_the_registry_is_not_marked(root):
    make(root, "vacio", branch="main", registry=False)

    summary = by_id(wr.list_workspaces(Req()))["vacio"]

    assert summary.is_incident_registry is False
    assert summary.reconciler_owned is False
