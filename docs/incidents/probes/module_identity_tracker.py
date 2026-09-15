"""Sonda de INC-2026-004 (SIG-sys-modules-package-shadowed-by-tests-dir).

Plugin de pytest que detecta, test por test, si algún módulo del producto o
alguno de sus atributos clave cambia de identidad (id()) durante el lote:
eso es exactamente lo que hacían los tests que instalaban Mocks en
sys.modules, borraban módulos reales o cargaban el producto por ruta de
archivo bajo un segundo nombre. Señal sana: ninguna línea MODTRACK.

Uso (desde src/backend):
  PYTHONPATH=$PWD/../../docs/incidents/probes:$PWD/../..:$PWD \\
    uv run python -m pytest ../../src/tests/backend -q -p module_identity_tracker 2>&1 | grep MODTRACK
"""

import sys

import pytest

MODS = (
    "v4.config.settings",
    "v4.models.messages",
    "common.config.app_config",
    "v4.callbacks.response_handlers",
    "common.database.cosmosdb",
    "v4.orchestration.orchestration_manager",
    "v4.common.services.team_service",
    "auth.auth_utils",
    "app",
)
ATTRS = (
    ("v4.config.settings", "logger"),
    ("v4.config.settings", "config"),
    ("v4.config.settings", "connection_config"),
    ("v4.config.settings", "orchestration_config"),
    ("v4.config.settings", "ConnectionConfig"),
    ("common.config.app_config", "config"),
)


def _snapshot():
    snap = {m: id(sys.modules[m]) for m in MODS if m in sys.modules}
    for m, a in ATTRS:
        mod = sys.modules.get(m)
        if mod is not None:
            snap[f"{m}.{a}"] = id(getattr(mod, a, None))
    return snap


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    before = _snapshot()
    yield
    after = _snapshot()
    for key in sorted(set(before) | set(after)):
        if key in before and key in after and before[key] != after[key]:
            print(f"\nMODTRACK {item.nodeid}: {key} REEMPLAZADO", file=sys.stderr)
        elif key in before and key not in after:
            print(f"\nMODTRACK {item.nodeid}: {key} BORRADO", file=sys.stderr)
