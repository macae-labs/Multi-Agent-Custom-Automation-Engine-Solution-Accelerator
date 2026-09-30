"""Borrar y volver a crear un workspace desde su repo, sin resurrecciones.

Cadena medida en prod 2026-09-27: el usuario borra el workspace, el frontend
sigue consultando el que quedó seleccionado, esa LECTURA lo vuelve a crear con
``git init`` (``workspace_for`` creaba en diferido), y el ``POST /workspaces``
siguiente ve ese ``.git``, entra por la rama de re-apertura, NO clona y
devuelve 201 con el árbol vacío. Dos artefactos lo probaron: el reflog con una
sola entrada ``commit (initial): init workspace`` y un metadato sin
``created_at`` (sólo la re-apertura lo omite).

Repo de verdad en ``tmp_path``; el clon se sustituye porque ``_SAFE_REPO_URL``
sólo admite https y acá no hay red.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi import HTTPException

import v4.api.workspace_router as wr
import v4.common.services.workspace_service as ws_mod
import v4.control.workspace_capability as wc

USER = "lifecycle-user"
WS = "mi-repo"
URL = "https://github.com/macae-labs/Multi-Agent-Custom-Automation-Engine-Solution-Accelerator.git"


class Req:
    headers = {"x-ms-client-principal-id": USER}


@pytest.fixture
def root(tmp_path, monkeypatch):
    base = tmp_path / "workspaces"
    monkeypatch.setattr(ws_mod, "WORKSPACE_ROOT", base)
    monkeypatch.setattr(wc, "WORKSPACE_ROOT", base)
    monkeypatch.setattr(wr, "WORKSPACE_ROOT", base)
    monkeypatch.setattr(wr, "_auth_user", lambda request: USER)
    return base


def fake_clone(monkeypatch) -> list[tuple[Path, str]]:
    """Doble de ``_clone_into``: deja un repo con un archivo, como un clon real."""
    calls: list[tuple[Path, str, str | None]] = []

    def _clone(ws: Path, url: str, token: str | None, branch: str | None = None) -> None:
        calls.append((ws, url, branch))
        ws.mkdir(parents=True, exist_ok=True)
        init = ["git", "init", "-q"] + (["-b", branch] if branch else [])
        subprocess.run(init, cwd=ws, check=True, capture_output=True)
        (ws / "README.md").write_text("clonado")

    monkeypatch.setattr(wr, "_clone_into", _clone)
    return calls


def create(name=WS, *, repo_url=URL, branch=None):
    return wr.create_workspace(
        Req(), wr.WorkspaceCreateRequest(name=name, repo_url=repo_url, branch=branch)
    )


def test_a_read_never_gives_birth_to_a_workspace(root):
    """Un GET sobre un workspace que no existe es 404, y NO lo crea: crear desde
    una lectura era lo que resucitaba el que el usuario acababa de borrar."""
    with pytest.raises(HTTPException) as err:
        wr.list_files(Req(), "no-existe")

    assert err.value.status_code == 404
    assert not (root / USER / "no-existe").exists()


def test_creating_after_a_delete_clones_instead_of_reopening(root, monkeypatch):
    calls = fake_clone(monkeypatch)
    create()
    assert len(calls) == 1
    wr.delete_workspace(Req(), WS)

    # El frontend sigue consultando el workspace seleccionado justo después de
    # borrarlo: eso no puede dejar nada en pie.
    with pytest.raises(HTTPException):
        wr.list_files(Req(), WS)

    response = create()

    assert len(calls) == 2, "el segundo create tiene que clonar, no re-abrir"
    assert response.file_count >= 1
    assert (root / USER / WS / "README.md").read_text() == "clonado"


def test_a_removal_that_cannot_finish_still_frees_the_name(root, monkeypatch):
    """Azure Files corta el borrado con ENOTEMPTY (listado rancio). El árbol se
    aparta con un rename, así que el nombre queda libre igual y el create
    siguiente clona en vez de re-abrir un cadáver con ``.git``."""
    calls = fake_clone(monkeypatch)
    create()
    ws = root / USER / WS
    monkeypatch.setattr(wr.shutil, "rmtree", lambda *a, **k: None)  # nunca borra

    wr.delete_workspace(Req(), WS)

    assert not ws.exists(), "el nombre del usuario tiene que quedar libre"
    apartados = [p.name for p in (root / USER).iterdir() if p.name.startswith(".")]
    assert len(apartados) == 1 and apartados[0].startswith(f".deleting-{WS}-")

    monkeypatch.setattr(wr.shutil, "rmtree", shutil.rmtree)
    create()

    assert len(calls) == 2
    assert (ws / "README.md").read_text() == "clonado"


def test_the_leftovers_of_a_failed_removal_are_not_a_second_registry(root, monkeypatch):
    """Un resto ``.deleting-*`` con docs/incidents no puede volver ambiguo al
    descubrimiento: el nombre empieza con punto y no es un workspace_id válido."""
    fake_clone(monkeypatch)
    create()
    ws = root / USER / WS
    (ws / "docs" / "incidents").mkdir(parents=True)
    (ws / "docs" / "incidents" / "INC-2026-001.x.json").write_text(
        '{"incident_id": "INC-2026-001"}'
    )
    monkeypatch.setattr(wr.shutil, "rmtree", lambda *a, **k: None)
    wr.delete_workspace(Req(), WS)

    assert wc.discover() is None


def test_the_declared_branch_is_cloned_and_recorded_in_the_workspace_meta(root, monkeypatch):
    """La rama es intención del usuario al montar y parte del contrato del
    workspace: el clon nace en ella y queda en el meta, que es de donde parten
    el sandbox del MCP (`upstream`, `--branch`, publish) y el fast-forward del
    registro. Sin esto todo lo de abajo sincronizaba el HEAD circunstancial."""
    calls = fake_clone(monkeypatch)

    create(branch="stable/v4-baseline")

    assert calls[0][2] == "stable/v4-baseline"
    meta = json.loads((root / USER / WS / ws_mod._META_FILE).read_text())
    assert meta["repo_url"] == URL
    assert meta["branch"] == "stable/v4-baseline"


def test_without_a_declared_branch_the_resolved_one_is_still_recorded(root, monkeypatch):
    fake_clone(monkeypatch)

    create()

    meta = json.loads((root / USER / WS / ws_mod._META_FILE).read_text())
    assert meta["branch"]  # la por defecto del remoto, pero registrada
