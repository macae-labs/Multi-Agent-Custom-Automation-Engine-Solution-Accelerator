"""Sonda local del incremento 4: la entrada REAL INC-2026-004 por el pipeline real.

Clase read-only sobre el repo; el control negativo es write-scratch en /tmp
(git worktree). Sin Cosmos: ``EventStore`` en memoria con la misma semántica.
La capacidad de ejecución es un shell local rooteado en el árbol bajo prueba;
en producción es ``workspace_exec`` de ca-mcp.

Uso:  cd src/backend && uv run python ../../docs/incidents/probes/incident_revalidation_probe.py

Control positivo: INC-2026-004 vencido → ``incident_expiry`` → sonda (suite con
``-p module_identity_tracker``, ``find __init__.py``, ``sys.path.insert``) → exit 0
→ ``reconciled`` operational=true.
Control negativo: worktree de HEAD con ``src/tests/backend/auth/__init__.py``
restaurado (la firma de INC-004) → la misma sonda sale con exit≠0 →
``needs_revalidation``, operational=false.
"""

import asyncio
import json
import pathlib
import subprocess
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3] / "src/backend"))

from common.services.event_store import EventStore, MemoryContainer  # noqa: E402
from v4.control import incident_revalidation as ir  # noqa: E402
from v4.control.reconciler import Reconciler  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[3]
ENTRY = ROOT / "docs/incidents/INC-2026-004.tests-package-shadows-backend-auth.json"
WORKTREE = pathlib.Path("/tmp/inc4-negative-control")
FAILED = 0


def check(label, ok, detail=""):
    global FAILED
    FAILED += 0 if ok else 1
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail}".rstrip())


def local_shell(root: pathlib.Path) -> ir.Executor:
    async def execute(command: str, cwd: str) -> ir.Evidence:
        proc = await asyncio.create_subprocess_shell(
            command, cwd=str(root / cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        out, err = await proc.communicate()
        return ir.Evidence(proc.returncode or 0, out.decode(), err.decode())

    return execute


async def revalidate(root: pathlib.Path, label: str):
    incident = json.loads(ENTRY.read_text())
    now = ir._parse(incident["learn"]["expires_if_not_reverified_by"]) + timedelta(days=1)
    store = EventStore(container=MemoryContainer())

    async def registry():
        return [incident]

    rec = Reconciler(store=store, registry=registry, execute=local_shell(root), now=lambda: now)
    await rec.run_once()
    expiry = await store.find(ir.KIND_EXPIRY, ir.expiry_identity(incident))
    check(f"{label} incident_expiry aplicado", expiry is not None and expiry["status"] == "applied", f"status={expiry and expiry['status']}")
    state = await ir.operational_state(store, incident)
    tail = (state or {}).get("evidence", {}).get("stdout", "").strip().splitlines()[-1:] if state else []
    return state, tail


async def main():
    state, tail = await revalidate(ROOT, "positivo")
    check("positivo operational=true", bool(state) and state["operational"] is True, f"{tail}")

    subprocess.run(["git", "worktree", "remove", "--force", str(WORKTREE)], cwd=ROOT, capture_output=True)
    subprocess.run(["git", "worktree", "add", "--detach", str(WORKTREE), "HEAD"], cwd=ROOT, check=True, capture_output=True)
    try:
        (WORKTREE / "src/tests/backend/auth/__init__.py").write_text("")
        subprocess.run(["uv", "sync", "--frozen"], cwd=WORKTREE / "src/backend", check=True, capture_output=True)
        state, tail = await revalidate(WORKTREE, "negativo")
        check("negativo needs_revalidation", bool(state) and state["operational"] is False and state["status"] == "needs_revalidation", f"{tail}")
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(WORKTREE)], cwd=ROOT, capture_output=True)
    print(f"{'OK' if FAILED == 0 else 'FALLOS: ' + str(FAILED)}")
    return FAILED


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
