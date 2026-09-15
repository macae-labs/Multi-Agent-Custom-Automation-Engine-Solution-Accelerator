"""Cleanup IDENTIFICADO de sesiones de prueba en Cosmos de producción (INC-2026-003).

Clase: write-shared · radio: shared_prod_data · destructivo · human_required.
Por defecto es DRY-RUN: enumera y verifica, no borra. Sólo borra con
`--approve <texto>` explícito, y borra EXCLUSIVAMENTE los (user_id, session_id)
listados en el inventario versionado; luego verifica que cada id listado ya no
existe y que ningún otro id de esos usuarios desapareció.

Uso (desde src/backend, con .env y az login):
  PYTHONPATH=$PWD uv run python ../../docs/incidents/probes/cleanup_prod_test_sessions.py \\
      ../../docs/incidents/INC-2026-003.prod-test-sessions.json            # dry-run
  PYTHONPATH=$PWD uv run python ../../docs/incidents/probes/cleanup_prod_test_sessions.py \\
      ../../docs/incidents/INC-2026-003.prod-test-sessions.json --approve "aprobado por Winston"

Salida: JSON con before/after por usuario, ids borrados, ids que faltaron y
`untouched_ok` (true si no desapareció ningún id fuera del inventario), listo
para copiar en la acción `cleanup-prod-test-sessions` de INC-2026-003.
"""

import asyncio
import datetime
import json
import sys

from common.services.chat_cosmos_service import get_chat_cosmos_service


async def _ids(svc, user_id):
    return {(s.get("session_id") or s.get("id")) for s in await svc.get_sessions_by_user(user_id)}


async def main(inventory_path, approval):
    with open(inventory_path, encoding="utf-8") as f:
        inv = json.load(f)
    svc = await get_chat_cosmos_service()
    report = {"dry_run": approval is None, "approval": approval, "started_at": datetime.datetime.utcnow().isoformat() + "Z", "users": {}}
    for user_id, listed in inv["sessions_by_user"].items():
        before = await _ids(svc, user_id)
        missing_before = sorted(set(listed) - before)
        deleted = []
        if approval is not None:
            for sid in listed:
                if sid in before:
                    await svc._container.delete_item(item=sid, partition_key=user_id)
                    deleted.append(sid)
        after = await _ids(svc, user_id)
        untouched_ok = (before - set(listed)) == (after - set(listed))
        report["users"][user_id] = {
            "label": inv.get("labels", {}).get(user_id, ""),
            "before": len(before), "after": len(after), "listed": len(listed),
            "deleted": deleted, "missing_before": missing_before,
            "still_present": sorted(set(listed) & after),
            "untouched_ok": untouched_ok,
        }
    report["finished_at"] = datetime.datetime.utcnow().isoformat() + "Z"
    report["all_untouched_ok"] = all(u["untouched_ok"] for u in report["users"].values())
    report["all_listed_gone"] = all(not u["still_present"] for u in report["users"].values())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if (report["dry_run"] or (report["all_untouched_ok"] and report["all_listed_gone"])) else 1


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(2)
    path = args[0]
    approval = None
    if "--approve" in args:
        i = args.index("--approve")
        approval = args[i + 1] if i + 1 < len(args) else ""
        if not approval:
            print("--approve requiere un texto de aprobación explícito")
            sys.exit(2)
    sys.exit(asyncio.run(main(path, approval)))
