"""El registro de incidentes cumple su propio contrato.

`docs/incidents/schema/incident.v1.schema.json` existía pero NADA lo hacía
cumplir, así que ya había deriva: un documento de inventario (`purpose`,
`inventoried_at`, `sessions_by_user`) vivía como `INC-2026-003.*.json`,
reusando el id de un incidente real y cargándose como incidente porque tiene
`incident_id`. Medido 2026-09-28: 10 de 11 archivos validaban.

Se valida lo que el reconciliador REALMENTE lee: `docs/incidents/*.json` no
recursivo, igual que `WorkspaceCapability.registry()` y `discover()`.
"""

import json
import pathlib

import pytest
from jsonschema import Draft202012Validator

from v4.control.incident_revalidation import ACTION_CLASSES, _parse

ROOT = pathlib.Path(__file__).resolve().parents[5]
REGISTRY = ROOT / "docs" / "incidents"
SCHEMA = json.loads(
    (REGISTRY / "schema" / "incident.v1.schema.json").read_text(encoding="utf-8")
)
ENTRIES = sorted(REGISTRY.glob("*.json"))


def ids(paths):
    return [p.name for p in paths]


def test_the_registry_is_not_empty():
    assert ENTRIES, f"sin incidentes en {REGISTRY}"


@pytest.mark.parametrize("path", ENTRIES, ids=ids(ENTRIES))
def test_every_entry_validates_against_the_schema(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(doc), key=str)
    assert not errors, "\n".join(
        f"{list(e.path) or '<raíz>'}: {e.message}" for e in errors[:5]
    )


def test_incident_ids_are_unique():
    """Dos archivos con el mismo ``incident_id`` compiten por la identidad del
    evento (`<incident_id>:<expires…>`): el segundo choca con 409 y su
    revalidación nunca corre."""
    seen: dict[str, str] = {}
    duplicados = []
    for path in ENTRIES:
        inc = json.loads(path.read_text(encoding="utf-8"))["incident_id"]
        if inc in seen:
            duplicados.append(f"{inc}: {seen[inc]} y {path.name}")
        seen[inc] = path.name
    assert not duplicados, duplicados


@pytest.mark.parametrize("path", ENTRIES, ids=ids(ENTRIES))
def test_the_ceiling_is_one_the_reconciler_can_compare(path):
    """``exceeds_ceiling`` hace ``ACTION_CLASSES.index(...)``: un valor fuera de
    la escala revienta la transición con ValueError, no con un veredicto."""
    ceiling = json.loads(path.read_text(encoding="utf-8"))["authority_ceiling"]
    assert ceiling["max_action_class_without_human"] in ACTION_CLASSES


@pytest.mark.parametrize("path", ENTRIES, ids=ids(ENTRIES))
def test_an_executable_probe_carries_a_parseable_expiry_and_its_class(path):
    """La sonda es lo que origina trabajo. Si la declara, su fecha tiene que ser
    legible (``rearm_due`` la compara) y su clase comparable contra el techo."""
    learn = json.loads(path.read_text(encoding="utf-8"))["learn"]
    if not learn.get("executable_probe"):
        pytest.skip("sin sonda ejecutable: no origina trabajo")
    _parse(learn["expires_if_not_reverified_by"])
    assert learn["executable_probe"]["class"] in ACTION_CLASSES


# ── el binding de alerta tiene que ser determinista ──────────────────────────
# `alert_match` es el selector de candidato. Si dos entradas declaran el mismo
# binding, o si el de una está CONTENIDO en el de otra, toda alerta que empareje
# con la más específica empareja también con la más general: detección ambigua
# con incident_id únicos. La unicidad de ids no alcanza; la del binding sí.


def _bindings():
    for path in ENTRIES:
        doc = json.loads(path.read_text(encoding="utf-8"))
        for binding in doc["signature"].get("alert_match") or []:
            yield doc["incident_id"], binding


def test_no_two_incidents_share_or_subsume_an_alert_binding():
    todos = list(_bindings())
    ambiguos = []
    for i, (inc_a, a) in enumerate(todos):
        for inc_b, b in todos[i + 1 :]:
            if inc_a == inc_b or a["rule"] != b["rule"]:
                continue
            da, db = a["dimensions"], b["dimensions"]
            contenido = all(db.get(k) == v for k, v in da.items()) or all(
                da.get(k) == v for k, v in db.items()
            )
            if contenido:
                ambiguos.append(f"{inc_a} y {inc_b} sobre {a['rule']}: {da} vs {db}")
    assert not ambiguos, ambiguos


def test_every_binding_names_a_rule_that_exists_in_the_project():
    """Las reglas son el alfabeto externo: un nombre que no existe nunca
    emparejará, y el registro diría que cubre algo que no cubre."""
    reglas = {
        "macae-api-5xx",
        "macae-excepciones",
        "macae-dependencias",
        "macae-traces-error",
    }
    desconocidas = sorted(
        {b["rule"] for _, b in _bindings() if b["rule"] not in reglas}
    )
    assert not desconocidas, desconocidas
